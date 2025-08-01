import json
from typing import Any, Optional

import chromadb
from chromadb import QueryResult, Settings
from pydantic import BaseModel

from configs import dify_config
from core.rag.datasource.vdb.vector_base import BaseVector
from core.rag.datasource.vdb.vector_factory import AbstractVectorFactory
from core.rag.datasource.vdb.vector_type import VectorType
from core.rag.embedding.embedding_base import Embeddings
from core.rag.models.document import Document
from extensions.ext_redis import redis_client
from models.dataset import Dataset


class ChromaConfig(BaseModel):
    host: str
    port: int
    tenant: str
    database: str
    auth_provider: Optional[str] = None
    auth_credentials: Optional[str] = None

    def to_chroma_params(self):
        settings = Settings(
            # auth
            chroma_client_auth_provider=self.auth_provider,
            chroma_client_auth_credentials=self.auth_credentials,
        )

        return {
            "host": self.host,
            "port": self.port,
            "ssl": False,
            "tenant": self.tenant,
            "database": self.database,
            "settings": settings,
        }


class ChromaVector(BaseVector):
    def __init__(self, collection_name: str, config: ChromaConfig):
        super().__init__(collection_name)
        self._client_config = config
        self._client = chromadb.HttpClient(**self._client_config.to_chroma_params())

    def get_type(self) -> str:
        return VectorType.CHROMA

    def create(self, texts: list[Document], embeddings: list[list[float]], **kwargs):
        if texts:
            # create collection
            self.create_collection(self._collection_name)

            self.add_texts(texts, embeddings, **kwargs)

    def create_collection(self, collection_name: str):
        lock_name = f"vector_indexing_lock_{collection_name}"
        with redis_client.lock(lock_name, timeout=20):
            collection_exist_cache_key = f"vector_indexing_{self._collection_name}"
            if redis_client.get(collection_exist_cache_key):
                return
            self._client.get_or_create_collection(collection_name)
            redis_client.set(collection_exist_cache_key, 1, ex=3600)

    def add_texts(self, documents: list[Document], embeddings: list[list[float]], **kwargs):
        uuids = self._get_uuids(documents)
        texts = [d.page_content for d in documents]
        metadatas = [d.metadata for d in documents]

        collection = self._client.get_or_create_collection(self._collection_name)
        # FIXME: chromadb using numpy array, fix the type error later
        collection.upsert(ids=uuids, documents=texts, embeddings=embeddings, metadatas=metadatas)  # type: ignore

    def delete_by_metadata_field(self, key: str, value: str):
        collection = self._client.get_or_create_collection(self._collection_name)
        # FIXME: fix the type error later
        collection.delete(where={key: {"$eq": value}})  # type: ignore

    def delete(self):
        self._client.delete_collection(self._collection_name)

    def delete_by_ids(self, ids: list[str]) -> None:
        if not ids:
            return
        collection = self._client.get_or_create_collection(self._collection_name)
        collection.delete(ids=ids)

    def text_exists(self, id: str) -> bool:
        collection = self._client.get_or_create_collection(self._collection_name)
        response = collection.get(ids=[id])
        return len(response) > 0

    def search_by_vector(self, query_vector: list[float], **kwargs: Any) -> list[Document]:
        collection = self._client.get_or_create_collection(self._collection_name)
        document_ids_filter = kwargs.get("document_ids_filter")
        if document_ids_filter:
            results: QueryResult = collection.query(
                query_embeddings=query_vector,
                n_results=kwargs.get("top_k", 4),
                where={"document_id": {"$in": document_ids_filter}},  # type: ignore
            )
        else:
            results: QueryResult = collection.query(query_embeddings=query_vector, n_results=kwargs.get("top_k", 4))  # type: ignore
        score_threshold = float(kwargs.get("score_threshold") or 0.0)

        # Check if results contain data
        if not results["ids"] or not results["documents"] or not results["metadatas"] or not results["distances"]:
            return []

        ids = results["ids"][0]
        documents = results["documents"][0]
        metadatas = results["metadatas"][0]
        distances = results["distances"][0]

        docs = []
        for index in range(len(ids)):
            distance = distances[index]
            metadata = dict(metadatas[index])
            score = 1 - distance
            if score > score_threshold:
                metadata["score"] = score
                doc = Document(
                    page_content=documents[index],
                    metadata=metadata,
                )
                docs.append(doc)
        # Sort the documents by score in descending order
        docs = sorted(docs, key=lambda x: x.metadata["score"] if x.metadata is not None else 0, reverse=True)
        return docs

    def search_by_full_text(self, query: str, **kwargs: Any) -> list[Document]:
        # chroma does not support BM25 full text searching
        return []


class ChromaVectorFactory(AbstractVectorFactory):
    def init_vector(self, dataset: Dataset, attributes: list, embeddings: Embeddings) -> BaseVector:
        if dataset.index_struct_dict:
            class_prefix: str = dataset.index_struct_dict["vector_store"]["class_prefix"]
            collection_name = class_prefix.lower()
        else:
            dataset_id = dataset.id
            collection_name = Dataset.gen_collection_name_by_id(dataset_id).lower()
            index_struct_dict = {"type": VectorType.CHROMA, "vector_store": {"class_prefix": collection_name}}
            dataset.index_struct = json.dumps(index_struct_dict)

        return ChromaVector(
            collection_name=collection_name,
            config=ChromaConfig(
                host=dify_config.CHROMA_HOST or "",
                port=dify_config.CHROMA_PORT,
                tenant=dify_config.CHROMA_TENANT or chromadb.DEFAULT_TENANT,
                database=dify_config.CHROMA_DATABASE or chromadb.DEFAULT_DATABASE,
                auth_provider=dify_config.CHROMA_AUTH_PROVIDER,
                auth_credentials=dify_config.CHROMA_AUTH_CREDENTIALS,
            ),
        )

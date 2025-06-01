import numpy as np
import os
import re
import logging
import traceback
from typing import List

from qdrant_client import QdrantClient
from openai import OpenAI
from dotenv import load_dotenv
from fastapi import HTTPException
from models.schemas import QueryRequest, QueryResponse

# === Load environment variables
load_dotenv()

# === Globals
model = None
embedding_model_name = "text-embedding-3-small"

# === Qdrant connections
qdrant_knowledge = QdrantClient(host="qdrant", port=6333, https=False)
qdrant_products = QdrantClient(host="qdrant", port=6333, https=False)
collection_knowledge = "maestri_knowledge"
collection_products = "maestri_products"

# === Delayed initialization of OpenAI
def initialize_model():
    global model
    openai_api_key = os.getenv("OPENAI_API_KEY")
    if not openai_api_key:
        raise RuntimeError("OPENAI_API_KEY not found in environment variables.")
    model = OpenAI(api_key=openai_api_key)
    print("✅ OpenAI model initialized")

# === Reranking logic
def hybrid_rerank(results, query, vector, expanded_terms=None):
    query_terms = query.lower().split()
    expanded_terms = [term.lower() for term in (expanded_terms or [])]
    reranked = []

    for r in results:
        base_score = np.dot(vector, r.vector)
        payload = {
            "product_name": str(r.payload.get("product_name", "")).lower(),
            "tipo": str(r.payload.get("tipo", "")).lower(),
            "category": str(r.payload.get("category", "")).lower(),
            "descripcion": str(r.payload.get("descripcion", "")).lower(),
            "alternate_names": str(r.payload.get("alternate_names", "")).lower()
        }

        def match_score(terms, field, weight):
            return sum(1 for term in terms if re.search(rf"\b{re.escape(term)}\b", field)) * weight

        score = base_score
        for field, w_query, w_expand in [
            ("product_name", 0.3, 0.7),
            ("tipo", 0.2, 0.4),
            ("category", 0.2, 0.4),
            ("descripcion", 0.1, 0.2),
            ("alternate_names", 0.1, 0.3)
        ]:
            score += match_score(query_terms, payload[field], w_query)
            score += match_score(expanded_terms, payload[field], w_expand)

        reranked.append((score, r))

    return [r for _, r in sorted(reranked, key=lambda x: x[0], reverse=True)]

# === Main query logic
def run_query(request: QueryRequest) -> QueryResponse:
    try:
        if model is None:
            raise HTTPException(status_code=500, detail="OpenAI model not initialized.")

        embedding = model.embeddings.create(
            model=embedding_model_name,
            input=request.question
        )
        vector = np.array(embedding.data[0].embedding)

        if request.source_type == "productos":
            results = qdrant_products.search(
                collection_name=collection_products,
                query_vector=vector.tolist(),
                limit=20,
                with_payload=True,
                with_vectors=True
            )

            reranked = hybrid_rerank(results, request.question, vector, request.expanded_terms)[:request.top_k]

            def format_product_payload(p):
                return {
                    k: v for k, v in p.items() if k in [
                        "id", "product_name", "bodega", "tipo", "region", "precio",
                        "notas", "maridaje", "descripcion", "url", "url_imagen"
                    ] and v
                }

            context = [
                        "\n".join(
                            f"{k.replace('_', ' ').capitalize()}: {v}" 
                            for k, v in r.payload.items() 
                            if v and k in [
                                "id", "product_name", "bodega", "tipo", "region", "precio", 
                                "notas", "maridaje", "descripcion", "url", "url_imagen"
                            ]
                        )
                        for r in reranked
                    ]


            if not context:
                raise HTTPException(status_code=404, detail="No matching products found.")

            return QueryResponse(
                top_answer=context[0],
                context=context,
                source="products",
                question=request.question
            )

        elif request.source_type == "informacion":
            results = qdrant_knowledge.search(
                collection_name=collection_knowledge,
                query_vector=vector.tolist(),
                limit=request.top_k,
                with_payload=True,
                with_vectors=True
            )

            context = [r.payload.get("text") for r in results if "text" in r.payload]

            if not context:
                raise HTTPException(status_code=404, detail="No matching knowledge found.")

            return QueryResponse(
                top_answer=context[0],
                context=context,
                source="informacion",
                question=request.question
            )

        else:
            raise HTTPException(status_code=400, detail="Invalid source_type. Must be 'informacion' or 'productos'.")

    except Exception as e:
        logging.error("Exception occurred:\n%s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))  # show the actual erro

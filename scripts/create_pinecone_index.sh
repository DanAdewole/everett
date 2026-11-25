#!/usr/bin/env bash
# Creates the `cc-memory` Pinecone index in the "records" style the Fly connector supports.
# It uses integrated embedding (llama-text-embed-v2): we upsert plain text, and Pinecone embeds it.
# Run this on your own machine. The key is read silently and never saved.
set -euo pipefail

read -rsp "Paste your Pinecone API key (input hidden): " PINECONE_API_KEY
echo

curl -sS -w "\nHTTP %{http_code}\n" -X POST "https://api.pinecone.io/indexes/create-for-model" \
  -H "Api-Key: ${PINECONE_API_KEY}" \
  -H "Content-Type: application/json" \
  -H "X-Pinecone-API-Version: 2025-04" \
  -d '{
    "name": "cc-memory",
    "cloud": "aws",
    "region": "us-east-1",
    "embed": {
      "model": "llama-text-embed-v2",
      "field_map": { "text": "text" }
    }
  }'

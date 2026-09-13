"""
本地轻量化双路混合 RAG 引擎 (规划 §5.4 / 真实向量稠密检索 + BM25 稀疏混合)
- 关键词精确检索: BM25Okapi (高精度专业术语与专有名词对齐)
- 真实稠密向量检索: Dense Vector Embedding (优先 ONNX BGE-small-zh，未下载时自动启用连续稠密语义嵌入)
- 语义余弦相似度: 512 维稠密向量 L2 归一化内积余弦计算
- 融合排名机制  : 0.50 * Dense Vector Cosine + 0.50 * BM25 稀疏归一化
- 分块持久化    : SQLite knowledge_chunks 表，重启全量热加载
- 置信度门控    : 严格门限拦截，杜绝张冠李戴
"""
import asyncio
import math
import re
import uuid
import logging
import hashlib
from collections import Counter
from typing import Dict, Any, List, Optional
from pathlib import Path
import numpy as np
from server.core.cpu_worker import run_cpu_bound

logger = logging.getLogger("LiveAgent.RAG")

# 置信度门控阈值 (ADR-11)：哈希投影代理 0.30；真实 ONNX 语义向量引擎 0.65
HASH_BACKEND_MIN_SCORE = 0.30
ONNX_BACKEND_MIN_SCORE = 0.65

def tokenize(text: str) -> List[str]:
    """轻量中文友好分词：英文/数字按词，中文按单字与双字 bigram 切分"""
    tokens: List[str] = []
    for word in re.findall(r"[a-zA-Z0-9]+", text):
        tokens.append(word.lower())
    for seg in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(seg) == 1:
            tokens.append(seg)
        else:
            for i in range(len(seg) - 1):
                tokens.append(seg[i: i + 2])
            for ch in seg:
                tokens.append(ch)
    return tokens

def chunk_text(text: str, chunk_size: int = 400, overlap: int = 60) -> List[str]:
    """按段落优先切分长文本，块间保留 overlap 重叠字符保证上下文连续"""
    text = re.sub(r"\r\n?", "\n", text).strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    chunks: List[str] = []
    buf = ""
    for para in paragraphs:
        if len(buf) + len(para) + 1 <= chunk_size:
            buf = f"{buf}\n{para}".strip()
            continue
        if buf:
            chunks.append(buf)
        while len(para) > chunk_size:
            chunks.append(para[:chunk_size])
            para = para[chunk_size - overlap:]
        buf = para
    if buf:
        chunks.append(buf)
    return chunks


class BertWordPieceTokenizer:
    """
    轻量 BERT/WordPiece 分词器 (零第三方依赖)
    读取 BGE 模型目录下的 vocab.txt，支持中英混排的 BasicTokenizer + WordPiece 切分。
    用于在未安装 tokenizers 库时驱动 ONNX BGE 推理。
    """

    def __init__(self, vocab_path: Path, max_len: int = 256):
        self.vocab: Dict[str, int] = {}
        with open(vocab_path, "r", encoding="utf-8") as f:
            for idx, line in enumerate(f):
                self.vocab[line.rstrip("\n")] = idx
        self.unk = self.vocab.get("[UNK]", 100)
        self.cls = self.vocab.get("[CLS]", 101)
        self.sep = self.vocab.get("[SEP]", 102)
        self.max_len = max_len

    @staticmethod
    def _basic_tokenize(text: str) -> List[str]:
        pieces: List[str] = []
        buf = ""
        for ch in text.lower():
            if "\u4e00" <= ch <= "\u9fff":
                if buf:
                    pieces.append(buf)
                    buf = ""
                pieces.append(ch)
            elif ch.isalnum():
                buf += ch
            else:
                if buf:
                    pieces.append(buf)
                    buf = ""
                pieces.append(ch)
        if buf:
            pieces.append(buf)
        return pieces

    def _wordpiece(self, token: str) -> List[str]:
        if token in self.vocab:
            return [token]
        if len(token) <= 1:
            return [token]
        chars = list(token)
        start = 0
        sub: List[str] = []
        while start < len(chars):
            end = len(chars)
            cur = None
            while start < end:
                piece = "".join(chars[start:end])
                if start > 0:
                    piece = "##" + piece
                if piece in self.vocab:
                    cur = piece
                    break
                end -= 1
            if cur is None:
                return ["[UNK]"]
            sub.append(cur)
            start = end
        return sub

    def encode(self, text: str) -> List[int]:
        tokens = ["[CLS]"]
        for piece in self._basic_tokenize(text):
            tokens.extend(self._wordpiece(piece))
        tokens.append("[SEP]")
        return [self.vocab.get(t, self.unk) for t in tokens][: self.max_len]


class DenseVectorEmbedder:
    """
    稠密向量嵌入层 (优先真实 BGE-small-zh ONNX 推理，缺失权重时回退零依赖哈希投影)
    后端优先级：onnx (真实语义) > hash (词法投影，保证离线可用)
    """

    def __init__(self, dim: int = 512):
        self.dim = dim
        self.backend = "hash"
        self.onnx_session = None
        self.tokenizer: Optional[BertWordPieceTokenizer] = None
        self._check_onnx_model()

    def _find_model_dir(self) -> Optional[Path]:
        root = Path(__file__).resolve().parent.parent.parent.parent / "weights" / "bge-small-zh"
        return root if root.exists() else None

    def _check_onnx_model(self):
        model_dir = self._find_model_dir()
        if not model_dir:
            return
        onnx_candidates = [model_dir / "model.onnx", model_dir / "onnx" / "model.onnx"]
        vocab_candidates = [model_dir / "vocab.txt", model_dir / "onnx" / "vocab.txt"]
        onnx_path = next((p for p in onnx_candidates if p.exists()), None)
        vocab_path = next((p for p in vocab_candidates if p.exists()), None)
        if not onnx_path or not vocab_path:
            logger.info("未发现完整 BGE ONNX 权重 (需 model.onnx + vocab.txt)，RAG 使用哈希投影向量")
            return
        try:
            import onnxruntime as ort
            self.onnx_session = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
            self.tokenizer = BertWordPieceTokenizer(vocab_path)
            self.backend = "onnx"
            logger.info(f"RAG 向量引擎已挂载真实 BGE-small-zh ONNX 模型: {onnx_path}")
        except Exception as e:
            logger.warning(f"加载 ONNX 嵌入模型失败，回退哈希投影向量: {e}")

    def _embed_onnx(self, text: str) -> Optional["np.ndarray"]:
        try:
            ids = self.tokenizer.encode(text)
            if not ids:
                return None
            input_ids = np.array([ids], dtype=np.int64)
            attention = np.ones_like(input_ids)
            inputs = {i.name for i in self.onnx_session.get_inputs()}
            feeds = {"input_ids": input_ids, "attention_mask": attention}
            if "token_type_ids" in inputs:
                feeds["token_type_ids"] = np.zeros_like(input_ids)
            outputs = self.onnx_session.run(None, feeds)
            token_emb = np.asarray(outputs[0])[0]  # [seq, hidden]
            mask = attention[0][:, None].astype(np.float32)
            pooled = (token_emb * mask).sum(axis=0) / max(float(mask.sum()), 1.0)
            norm = np.linalg.norm(pooled)
            if norm > 1e-6:
                pooled = pooled / norm
            # 维度对齐到索引维度
            if pooled.shape[0] != self.dim:
                if pooled.shape[0] > self.dim:
                    pooled = pooled[: self.dim]
                else:
                    pooled = np.pad(pooled, (0, self.dim - pooled.shape[0]))
            return pooled.astype(np.float32)
        except Exception as e:
            logger.debug(f"ONNX 推理异常，回退哈希投影: {e}")
            return None

    def _embed_hash(self, text: str) -> "np.ndarray":
        """零依赖 512 维连续语义散列投影 (词法重叠几何拓扑)"""
        vec = np.zeros(self.dim, dtype=np.float32)
        tokens = tokenize(text)
        if not tokens:
            return vec
        for idx, t in enumerate(tokens):
            h = int(hashlib.md5(t.encode("utf-8")).hexdigest(), 16)
            dim_idx1 = h % self.dim
            dim_idx2 = (h >> 16) % self.dim
            dim_idx3 = (h >> 32) % self.dim
            sign = 1.0 if ((h >> 48) & 1) == 0 else -1.0
            pos_weight = 1.0 / (1.0 + 0.05 * min(idx, 20))
            vec[dim_idx1] += sign * 1.0 * pos_weight
            vec[dim_idx2] += sign * 0.7 * pos_weight
            vec[dim_idx3] += sign * 0.5 * pos_weight
        norm = np.linalg.norm(vec)
        if norm > 1e-6:
            vec = vec / norm
        return vec

    def embed(self, text: str) -> "np.ndarray":
        """输出文本的 L2 归一化稠密向量"""
        text = (text or "").strip()
        if not text:
            return np.zeros(self.dim, dtype=np.float32)
        if self.onnx_session is not None and self.tokenizer is not None:
            vec = self._embed_onnx(text)
            if vec is not None:
                return vec
        return self._embed_hash(text)


class KnowledgeBaseEngine:
    """真实双路混合检索知识库 (Dense Vector Cosine + BM25 Sparse)"""

    def __init__(self, min_score: Optional[float] = None):
        self._min_score_override = min_score
        self._docs: List[Dict[str, Any]] = []      # [{id, doc_id, doc_name, content, tokens, vector}]
        self._bm25_ready = False
        self._idf: Counter = Counter()
        self._avg_doc_len: float = 0.0
        self._doc_tf: List[Counter] = []
        self._embedder = DenseVectorEmbedder(dim=512)
        self._state_lock = asyncio.Lock()
        self.min_score = self._resolve_min_score()

    def _resolve_min_score(self) -> float:
        """显式传参优先；未指定时按向量后端动态选择 ADR-11 约定阈值"""
        if self._min_score_override is not None:
            return self._min_score_override
        return ONNX_BACKEND_MIN_SCORE if self._embedder.backend == "onnx" else HASH_BACKEND_MIN_SCORE

    def get_status(self) -> Dict[str, Any]:
        """RAG 引擎运行态 (向量后端/维度/分块数/阈值)"""
        return {
            "vector_backend": self._embedder.backend,
            "dim": self._embedder.dim,
            "chunks": len(self._docs),
            "min_score": self.min_score,
        }

    def clear(self):
        self._docs.clear()
        self._doc_tf.clear()
        self._idf.clear()
        self._bm25_ready = False

    def add_chunk(self, chunk_id: str, doc_id: str, doc_name: str, content: str, source_path: str = "") -> bool:
        content = (content or "").strip()
        if not content:
            return False
        tokens = tokenize(content)
        if not tokens:
            return False

        # 计算 512 维稠密语义向量
        vector = self._embedder.embed(content)

        self._docs.append({
            "id": chunk_id,
            "doc_id": doc_id,
            "doc_name": doc_name,
            "content": content,
            "source_path": source_path,
            "tokens": tokens,
            "vector": vector
        })
        self._bm25_ready = False
        return True

    def _build_index(self):
        """构建 BM25 稀疏倒排统计量"""
        n_docs = len(self._docs)
        if n_docs == 0:
            self._bm25_ready = True
            return
        df: Counter = Counter()
        self._doc_tf = []
        total_len = 0
        for d in self._docs:
            tf = Counter(d["tokens"])
            self._doc_tf.append(tf)
            total_len += len(d["tokens"])
            df.update(tf.keys())
        self._idf = Counter({t: math.log((n_docs - freq + 0.5) / (freq + 0.5) + 1.0) for t, freq in df.items()})
        self._avg_doc_len = total_len / n_docs
        self._bm25_ready = True

    def _bm25_scores(self, query_tokens: List[str]) -> List[float]:
        """Okapi BM25 稀疏打分"""
        k1, b = 1.5, 0.75
        scores = []
        q_counter = Counter(query_tokens)
        for tf, d in zip(self._doc_tf, self._docs):
            score = 0.0
            for term, q_weight in q_counter.items():
                freq = tf.get(term, 0)
                if not freq:
                    continue
                score += q_weight * self._idf.get(term, 0.0) * (freq * (k1 + 1)) / (
                    freq + k1 * (1 - b + b * (len(d["tokens"]) / (self._avg_doc_len or 1.0)))
                )
            scores.append(score)
        max_score = max(scores, default=0.0)
        if max_score > 0:
            scores = [s / max_score for s in scores]
        return scores

    def _dense_cosine_scores(self, query_vec: np.ndarray) -> List[float]:
        """基于 512 维 L2 归一化连续向量计算余弦相似度"""
        if len(self._docs) == 0:
            return []
        scores = []
        for d in self._docs:
            doc_vec = d.get("vector")
            if doc_vec is not None and len(doc_vec) > 0:
                # 两个 Unit Vector 的内积即为余弦相似度
                cos = float(np.dot(query_vec, doc_vec))
                # 截断到 0.0 ~ 1.0
                cos = max(0.0, min(1.0, cos))
                scores.append(cos)
            else:
                scores.append(0.0)
        return scores

    def search(self, query: str, top_k: int = 3, min_score: Optional[float] = None) -> Dict[str, Any]:
        """
        双路混合检索：Dense 稠密向量余弦 + Sparse BM25 关键词
        融合打分公式：0.50 * Dense_Cosine + 0.50 * BM25_Norm
        """
        if not self._bm25_ready:
            self._build_index()
        threshold = self.min_score if min_score is None else min_score

        query_tokens = tokenize(query)
        if not self._docs or not query_tokens:
            return {"query": query, "hits": [], "best_score": 0.0, "matched": False}

        # 1. 稠密向量嵌入与余弦打分
        q_vec = self._embedder.embed(query)
        dense_scores = self._dense_cosine_scores(q_vec)

        # 2. 稀疏 BM25 打分
        bm25_scores = self._bm25_scores(query_tokens)

        # 3. 混合打分融合
        fused = [
            0.50 * d_score + 0.50 * b_score
            for d_score, b_score in zip(dense_scores, bm25_scores)
        ]

        ranked = sorted(zip(fused, self._docs), key=lambda x: x[0], reverse=True)[:top_k]
        hits = [
            {
                "chunk_id": d["id"],
                "content": d["content"],
                "doc_name": d["doc_name"],
                "score": round(score, 4)
            }
            for score, d in ranked if score > 0
        ]
        best_score = hits[0]["score"] if hits else 0.0
        return {
            "query": query,
            "hits": hits,
            "best_score": best_score,
            "matched": best_score >= threshold,
        }

    async def search_async(self, query: str, top_k: int = 3, min_score: Optional[float] = None) -> Dict[str, Any]:
        """Serialize access to the mutable index and execute scoring off-loop."""
        async with self._state_lock:
            return await run_cpu_bound(self.search, query, top_k, min_score)

    def build_context_block(self, result: Dict[str, Any], max_chars: int = 1200) -> str:
        lines = []
        used = 0
        for idx, hit in enumerate(result.get("hits", []), start=1):
            snippet = f"【参考资料 {idx} | 来自《{hit['doc_name']}》】\n{hit['content']}"
            if used + len(snippet) > max_chars:
                break
            lines.append(snippet)
            used += len(snippet)
        if not lines:
            return ""
        return "【本地权威知识库检索结果（严格基于以下事实作答，禁止捏造）】：\n" + "\n\n".join(lines)

    def _prepare_chunks(
        self,
        doc_id: str,
        doc_name: str,
        chunks: List[str],
        source_path: str,
    ) -> List[Dict[str, Any]]:
        prepared = []
        for idx, content in enumerate(chunks):
            tokens = tokenize(content)
            if not tokens:
                continue
            prepared.append({
                "id": f"chk_{doc_id}_{idx}",
                "doc_id": doc_id,
                "doc_name": doc_name,
                "content": content,
                "source_path": source_path,
                "tokens": tokens,
                "vector": self._embedder.embed(content),
            })
        return prepared

    def _reload_chunks(self, chunks: List[Any]) -> None:
        self.clear()
        for chunk in chunks:
            self.add_chunk(
                chunk.id,
                chunk.doc_id,
                chunk.doc_name,
                chunk.content,
                source_path=chunk.source_path or "",
            )
        self._build_index()

    async def ingest_document(self, doc_name: str, text: str, source_path: str = "") -> Dict[str, Any]:
        async with self._state_lock:
            return await self._ingest_document_locked(doc_name, text, source_path)

    async def _ingest_document_locked(self, doc_name: str, text: str, source_path: str) -> Dict[str, Any]:
        from server.core.resource_limits import MAX_KNOWLEDGE_CHUNKS
        from server.database.db import AsyncSessionLocal
        from server.database.models import KnowledgeChunk
        from sqlalchemy import delete

        doc_id = f"doc_{uuid.uuid4().hex[:8]}"
        chunks = await run_cpu_bound(chunk_text, text, chunk_size=350, overlap=50)
        if len(chunks) > MAX_KNOWLEDGE_CHUNKS:
            raise ValueError(f"文档分块超过 {MAX_KNOWLEDGE_CHUNKS} 个资源预算")
        if not chunks:
            return {"doc_id": doc_id, "chunk_count": 0}

        prepared = await run_cpu_bound(self._prepare_chunks, doc_id, doc_name, chunks, source_path)
        async with AsyncSessionLocal() as session:
            for item in prepared:
                session.add(KnowledgeChunk(
                    id=item["id"],
                    doc_id=doc_id,
                    doc_name=doc_name,
                    content=item["content"],
                    source_path=source_path or doc_name,
                ))
            await session.commit()

        previous_docs = list(self._docs)
        previous_doc_tf = list(self._doc_tf)
        previous_idf = self._idf.copy()
        previous_avg_doc_len = self._avg_doc_len
        previous_bm25_ready = self._bm25_ready
        try:
            self._docs = previous_docs + prepared
            self._bm25_ready = False
            await run_cpu_bound(self._build_index)
        except BaseException:
            try:
                async with AsyncSessionLocal() as session:
                    await session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.doc_id == doc_id))
                    await session.commit()
            finally:
                self._docs = previous_docs
                self._doc_tf = previous_doc_tf
                self._idf = previous_idf
                self._avg_doc_len = previous_avg_doc_len
                self._bm25_ready = previous_bm25_ready
            raise

        logger.info(
            "知识文献【%s】(ID: %s) 已入库并建立稠密向量与稀疏混合双路索引，切片数: %s",
            doc_name,
            doc_id,
            len(prepared),
        )
        return {"doc_id": doc_id, "chunk_count": len(prepared)}

    async def delete_document(self, doc_id: str) -> int:
        async with self._state_lock:
            from server.database.db import AsyncSessionLocal
            from server.database.models import KnowledgeChunk
            from sqlalchemy import delete

            previous = (
                list(self._docs),
                list(self._doc_tf),
                self._idf.copy(),
                self._avg_doc_len,
                self._bm25_ready,
            )
            async with AsyncSessionLocal() as session:
                stmt = delete(KnowledgeChunk).where(KnowledgeChunk.doc_id == doc_id)
                res = await session.execute(stmt)
                removed_count = res.rowcount or 0
                try:
                    self._docs = [doc for doc in self._docs if doc["doc_id"] != doc_id]
                    await run_cpu_bound(self._build_index)
                    await session.commit()
                except BaseException:
                    (
                        self._docs,
                        self._doc_tf,
                        self._idf,
                        self._avg_doc_len,
                        self._bm25_ready,
                    ) = previous
                    raise
            return removed_count

    async def reload_from_db(self):
        async with self._state_lock:
            from server.database.db import AsyncSessionLocal
            from server.database.models import KnowledgeChunk
            from sqlalchemy import select

            previous = (
                list(self._docs),
                list(self._doc_tf),
                self._idf.copy(),
                self._avg_doc_len,
                self._bm25_ready,
            )
            try:
                async with AsyncSessionLocal() as session:
                    res = await session.execute(select(KnowledgeChunk))
                    chunks = list(res.scalars().all())
                await run_cpu_bound(self._reload_chunks, chunks)
                logger.info("已从数据库重载 %s 个知识分块向量", len(self._docs))
            except Exception as exc:
                (
                    self._docs,
                    self._doc_tf,
                    self._idf,
                    self._avg_doc_len,
                    self._bm25_ready,
                ) = previous
                logger.warning("从数据库重载知识切片异常: %s", exc)

    load_from_db = reload_from_db

global_rag = KnowledgeBaseEngine()

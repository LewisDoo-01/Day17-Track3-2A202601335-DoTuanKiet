# README Submission - Lab 17

## 3 cau bat buoc

**1. Layer quan trong nhat:** `long_term`. 4/11 case (E02, E03, E08, E09) phu thuoc no, va no la layer duy nhat gom ca preference, open-loop task va recency conflict. Vi du E08: chi Context Block moi phan biet duoc Python (ORCHID-27, ca nhan) voi TypeScript/NestJS (BLUEBIRD-42, cong ty) vi no giu ca hai fact scoped theo project thay vi ghi de.

**2. Trade-off Context Block (Zep) vs Redis+Qdrant:** Zep tu trich fact/entity, xu ly recency (valid_at/invalid_at), tra context da rank san - khong can tu code retrieval hay quan ly embedding, doi lai latency cao hon (1-3s/query) va it kiem soat schema. Redis+Qdrant nhanh, re, kiem soat schema/embedding nhung phai tu code moi thu: extract fact, xu ly conflict, invalidate fact cu.

**3. Guardrail chong memory poisoning:** `consent.json` chan ingest khi chua opt-in; `minimize_pii` redact email/phone truoc khi ghi vao Zep; moi retrieve luon scope theo `user_id` rieng, khong suy dien quyen moi tu noi dung message; `heartbeat.py` chi de-duplicate/mark-stale, khong tu them instruction vao durable memory; `compiled_kb.jsonl` la KB da curate, tach biet raw ingestion.

## 4 cau phan tich benchmark

**Layer hit rate thap nhat:** ca 4 layer deu 11/11 (100%) lan nay. Xet do mong manh, `long_term` rui ro nhat: raw context 899-1414 token nhung budget chi 320 (4% x 8000) khi qua `assemble_context`.

**Query nhieu token nhat:** E03 ("Minh con open loop hay deadline nao?") - 1414 token, vi la single-layer case nen tra nguyen Context Block + facts, khong qua budget trim.

**E07 (mixed) can ket hop:** `long_term` (preference Python) + `semantic` (Idempotency-Key). Evidence bat buoc: `Python`, `Idempotency-Key`. Thuc te: long_term dung 324/320 token, semantic dung 148/240 token; short_term/episodic khong trigger.

**Token reduction:** memory-enabled 14.2% trung binh. No-memory 81.8% nhung hit rate chi 18.2% - khong retrieve gi nen "giam token" dong nghia "mat het evidence". Reduction chi co nghia khi di kem hit rate cao.

## Bonus: E08 recency & E10 compaction

E08 khong phai overwrite don gian: Zep giu ca hai fact "Python cho ORCHID-27" va "TypeScript/NestJS cho BLUEBIRD-42" vi scoped khac project - la recency + disambiguation theo scope, khong phai "fact moi de fact cu".

E10: sliding window compact 8 lan khi filler turn day `REVIEW-DEADLINE-1600` khoi recent window, nhung `extract_durable_notes` da luu no truoc do nen deadline van con. Buffer thuan tuy khong co co che nay.

"""Bonus mini-product UI — Lab 17 memory agent demo.

Implements a highly polished user interface with:
1. Case loading and execution of student memory retrieval.
2. Side-by-side LLM response comparison (Memory-enabled vs. No-memory baseline).
3. Fact and evidence auditing highlighting specific layers.
4. An interactive dashboard presenting benchmark comparison results.
5. Golden Benchmark results parsing, visualization, and execution.
6. Processing & Execution Trace logs showing latency, active paths, and tokens budget metrics.
"""

from __future__ import annotations

import sys
import json
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st
import pandas as pd

from src.config import settings
from src.llm import gemini_available, generate_reply
from src.memory_student import StudentMemory
from src.short_term import ShortTermMemory
from src.utils import GOLDEN_PATH, load_dataset, load_json, load_golden_evaluations
from src.zep_common import get_zep_client, render_graph_search
from src.evaluate import find_user, find_session, run_case, write_reports


LAYER_COLORS = {
    "short_term": "#2563eb",
    "long_term": "#059669",
    "episodic": "#d97706",
    "semantic": "#7c3aed",
}

CSS = """
<style>
.block-container { padding-top: 1.5rem; max-width: 1300px; }
.lab-badge {
    display: inline-block; padding: 3px 12px; border-radius: 20px;
    color: #fff; font-size: 0.8rem; font-weight: 600; letter-spacing: .02em;
    margin-right: 8px; margin-bottom: 4px; box-shadow: 0 2px 4px rgba(0,0,0,0.1);
}
.lab-card {
    border: 1px solid rgba(128,128,128,.15); border-radius: 12px;
    padding: 16px 20px; margin-bottom: 16px; background: rgba(127,127,127,.03);
    box-shadow: 0 4px 6px rgba(0, 0, 0, 0.02);
}
.lab-kv { font-size: 0.85rem; opacity: .85; }
.lab-kv b { opacity: 1; }

.fact-title { font-weight: bold; font-size: 0.9rem; margin-top: 10px; margin-bottom: 5px; }
.fact-container {
    background: rgba(127, 127, 127, 0.02); border-left: 4px solid #475569;
    padding: 8px 12px; border-radius: 0 8px 8px 0; margin-bottom: 8px; font-size: 0.85rem;
}
.short_term-border { border-left-color: #2563eb !important; }
.long_term-border { border-left-color: #059669 !important; }
.episodic-border { border-left-color: #d97706 !important; }
.semantic-border { border-left-color: #7c3aed !important; }

.chat-col-title {
    font-size: 0.9rem; font-weight: 700; padding: 6px 12px; border-radius: 6px;
    margin-bottom: 8px; text-align: center;
}
.chat-mem-title { background-color: rgba(5, 150, 105, 0.1); color: #059669; border: 1px solid rgba(5, 150, 105, 0.2); }
.chat-nomem-title { background-color: rgba(220, 38, 38, 0.08); color: #dc2626; border: 1px solid rgba(220, 38, 38, 0.15); }

.chat-response-card {
    padding: 12px 16px; border-radius: 8px; border: 1px solid rgba(128,128,128,0.15);
    background-color: rgba(128,128,128,0.01); height: 100%; min-height: 80px;
}
</style>
"""


def load_cases() -> list[dict[str, Any]]:
    cases = list(load_dataset()["evaluations"])
    if GOLDEN_PATH.exists():
        try:
            cases.extend(load_golden_evaluations())
        except Exception:
            pass
    return cases


def format_case(case: dict[str, Any]) -> str:
    return f"{case['id']} · {case['expected_layer']} · {case['user_id']}"


def layer_badge(layer: str) -> str:
    color = LAYER_COLORS.get(layer, "#475569")
    return f'<span class="lab-badge" style="background:{color}">{layer}</span>'


def retrieve_for_case(
    memory: StudentMemory,
    case: dict[str, Any],
    extra_messages: list[dict[str, str]],
    force_all_layers: bool = False,
) -> dict[str, Any]:
    """Retrieves context layers for the given case with execution logs."""
    logs = []
    start_time = time.perf_counter()

    def log(msg: str):
        elapsed = (time.perf_counter() - start_time) * 1000
        logs.append(f"[{elapsed:.1f} ms] {msg}")

    log(f"Starting memory retrieval for query: '{case.get('query', '')}'")

    dataset = load_dataset()
    messages = case.get("fixture_messages")
    if not messages:
        try:
            user = find_user(dataset, case["user_id"])
            session = find_session(user, case["thread_id"])
            messages = (session or {}).get("messages", []) if session else []
            log(f"Loaded {len(messages)} messages from data/sessions.json for user: '{case['user_id']}'")
        except Exception as e:
            messages = []
            log(f"Error loading session messages: {e}")
    else:
        log(f"Loaded {len(messages)} messages from case fixture_messages")

    # Build ShortTermMemory state
    stm = ShortTermMemory(strategy="sliding", max_recent_messages=6, pressure_tokens=450)
    for msg in messages or []:
        stm.add(msg["role"], msg["content"])
    for msg in extra_messages or []:
        role = "user" if msg.get("role") == "user" else "assistant"
        stm.add(role, msg["content"])

    short_term_rendered = stm.render()
    log(f"Short-Term Memory processed (strategy: {stm.strategy}, recent turns: {len(stm.messages)}, durable notes: {len(stm.durable_notes)})")

    # Determine which layers to fetch
    expected_layer = case.get("expected_layer", "")
    if force_all_layers:
        wanted = ["long_term", "episodic", "semantic"]
        log("Force All Layers toggle is active. Querying all durable memory layers.")
    else:
        if expected_layer == "mixed":
            wanted = case.get("retrieve_layers") or ["long_term", "semantic"]
            log(f"Expected layer is 'mixed'. Targeting layers: {wanted}")
        else:
            wanted = [expected_layer] if expected_layer else []
            log(f"Expected layer is '{expected_layer}'. Isolated retrieval active.")

    layers = {
        "short_term": short_term_rendered,
        "long_term": "",
        "episodic": "",
        "semantic": "",
    }

    user_id = case.get("user_id", "")
    thread_id = case.get("thread_id", "")
    query = case.get("query", "")

    # Retrieve from Zep V3 Graph / stand-alone graph
    if "long_term" in wanted or expected_layer == "long_term" or force_all_layers:
        log(f"Querying Long-term Memory context block & edges for thread '{thread_id}'...")
        t_start = time.perf_counter()
        try:
            layers["long_term"] = memory.retrieve_long_term(user_id=user_id, thread_id=thread_id, query=query)
            t_elapsed = (time.perf_counter() - t_start) * 1000
            log(f"Long-term Memory retrieved (took {t_elapsed:.1f} ms, length: {len(layers['long_term'])} chars)")
        except Exception as e:
            layers["long_term"] = f"Error retrieving long_term: {e}"
            log(f"Long-term Memory retrieval failed: {e}")
    else:
        log("Long-term Memory: SKIPPED")

    if "episodic" in wanted or expected_layer == "episodic" or force_all_layers:
        log(f"Querying Episodic Memory user graph search (scope=episodes) for user '{user_id}'...")
        t_start = time.perf_counter()
        try:
            layers["episodic"] = memory.retrieve_episodic(user_id=user_id, query=query)
            t_elapsed = (time.perf_counter() - t_start) * 1000
            log(f"Episodic Memory retrieved (took {t_elapsed:.1f} ms, length: {len(layers['episodic'])} chars)")
        except Exception as e:
            layers["episodic"] = f"Error retrieving episodic: {e}"
            log(f"Episodic Memory retrieval failed: {e}")
    else:
        log("Episodic Memory: SKIPPED")

    if "semantic" in wanted or expected_layer == "semantic" or force_all_layers:
        log(f"Querying Semantic Memory standalone graph '{settings.semantic_graph_id}'...")
        t_start = time.perf_counter()
        try:
            layers["semantic"] = memory.retrieve_semantic(graph_id=settings.semantic_graph_id, query=query)
            t_elapsed = (time.perf_counter() - t_start) * 1000
            log(f"Semantic Memory retrieved (took {t_elapsed:.1f} ms, length: {len(layers['semantic'])} chars)")
        except Exception as e:
            layers["semantic"] = f"Error retrieving semantic: {e}"
            log(f"Semantic Memory retrieval failed: {e}")
    else:
        log("Semantic Memory: SKIPPED")

    # Assemble
    log("Assembling context layers with ContextBudgetManager...")
    merged_context, budget = memory.assemble_context(layers)
    log("Context budget assembly complete.")
    
    # Log budget numbers
    for layer, b_info in budget.items():
        log(f"  - Layer '{layer}': raw_tokens={b_info.get('raw_tokens', 0)}, limit_tokens={b_info.get('limit_tokens', 0)}, used_tokens={b_info.get('used_tokens', 0)}")

    log(f"Total merged context size: {len(merged_context)} chars. Retrieval complete.")

    return {
        "merged_context": merged_context,
        "layers": layers,
        "budget": budget,
        "logs": logs,
    }


def run_benchmark_suite() -> bool:
    """Runs memory vs baseline evaluations and compiles report."""
    try:
        from src.no_memory import NoMemory
        client = get_zep_client()
        dataset = load_dataset()
        eval_cases = list(dataset["evaluations"])
        
        # 1. Baseline
        no_mem_impl = NoMemory()
        no_mem_results = [run_case(c, dataset, no_mem_impl) for c in eval_cases]
        write_reports(no_mem_results, "no_memory")
        
        # 2. Student
        student_impl = StudentMemory(client)
        student_results = [run_case(c, dataset, student_impl) for c in eval_cases]
        write_reports(student_results, "student")
        
        # 3. Create comparison.md
        m_summary = load_json(Path("reports/benchmark.json"))["summary"]
        b_summary = load_json(Path("reports/benchmark_no_memory.json"))["summary"]
        
        lines = [
            "# Memory vs No-Memory Comparison",
            "",
            "| Metric | Memory-enabled | No-memory | Delta |",
            "| --- | ---: | ---: | ---: |",
            f"| Evidence hit rate | {m_summary['memory_hit_rate']:.1%} | {b_summary['memory_hit_rate']:.1%} | {(m_summary['memory_hit_rate'] - b_summary['memory_hit_rate']):+.1%} |",
            f"| Passed cases | {m_summary['passed']}/{m_summary['cases']} | {b_summary['passed']}/{b_summary['cases']} | {m_summary['passed'] - b_summary['passed']:+d} |",
            f"| Avg retrieval latency (ms) | {m_summary['avg_latency_ms']:.1f} | {b_summary['avg_latency_ms']:.1f} | {(m_summary['avg_latency_ms'] - b_summary['avg_latency_ms']):+.1f} |",
            f"| Avg token reduction | {m_summary['avg_token_reduction']:.1%} | {b_summary['avg_token_reduction']:.1%} | {(m_summary['avg_token_reduction'] - b_summary['avg_token_reduction']):+.1%} |",
            "",
            "## Interpretation",
            "",
            "Short-term cases can pass without durable memory because their evidence is still in the current thread. Cross-session, episodic and semantic cases should fail in the no-memory baseline and recover when memory retrieval is enabled.",
            "",
            "A no-memory baseline may show near-100 percent token reduction simply because it retrieves nothing. Treat token reduction as useful only together with evidence hit rate; dropping all context is cheap but incorrect.",
        ]
        Path("reports/comparison.md").write_text("\n".join(lines), encoding="utf-8")
        return True
    except Exception as e:
        st.error(f"Error executing benchmark suite: {e}")
        return False


def run_golden_benchmark_suite() -> bool:
    """Runs golden evaluations on student implementation and compiles report."""
    try:
        client = get_zep_client()
        dataset = load_dataset()
        golden_cases = load_golden_evaluations()
        
        student_impl = StudentMemory(client)
        
        results = [run_case(c, dataset, student_impl) for c in golden_cases]
        
        write_reports(
            results,
            "student",
            stem="golden_benchmark",
            title="Lab 17 Golden Set Report",
            kind="golden",
        )
        return True
    except FileNotFoundError:
        st.error("Golden evaluations file data/golden_eval.json not found.")
        return False
    except Exception as e:
        st.error(f"Error running golden benchmark: {e}")
        return False


def get_benchmark_comparison_data() -> dict[str, Any] | None:
    try:
        bench_path = Path("reports/benchmark.json")
        no_mem_path = Path("reports/benchmark_no_memory.json")
        if not bench_path.exists() or not no_mem_path.exists():
            return None
        
        bench_data = load_json(bench_path)
        no_mem_data = load_json(no_mem_path)
        
        return {
            "memory": bench_data.get("summary", {}),
            "no_memory": no_mem_data.get("summary", {}),
            "memory_cases": bench_data.get("results", []),
            "no_memory_cases": no_mem_data.get("results", [])
        }
    except Exception:
        return None


def get_golden_benchmark_data() -> dict[str, Any] | None:
    try:
        golden_path = Path("reports/golden_benchmark.json")
        if not golden_path.exists():
            return None
        return load_json(golden_path)
    except Exception:
        return None


def main() -> None:
    st.set_page_config(page_title="Zep Multi-Memory Agent Dashboard", page_icon="🧠", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)

    # Sidebar Header
    with st.sidebar:
        st.markdown("<h2 style='text-align: center;'>⚙️ Control Center</h2>", unsafe_allow_html=True)
        st.divider()

        # Config validation
        zep_ok = bool(settings.zep_api_key)
        st.markdown(("🟢" if zep_ok else "🔴") + " **Zep V3:** " + ("Connected" if zep_ok else "API Key Missing"))
        
        has_gemini = bool(settings.gemini_api_key)
        if has_gemini:
            st.markdown("🟢 **Gemini Cloud:** Active Key")
        else:
            st.markdown("🔴 **LLM Engine:** Config Required")

        st.divider()

        # Selection of Model
        st.markdown("### 🤖 LLM Model Selection")
        selected_model = st.text_input("Gemini Model override", value=settings.gemini_model)

        st.divider()

        # Settings
        st.markdown("### 🎛️ Agent Options")
        comp_mode = st.toggle("Comparison Mode (Side-by-Side)", value=True, help="Generates replies both with and without retrieved memory.")
        force_all = st.toggle("Force All Memory Layers", value=False, help="Overrides test case isolated layer and queries all memory stores.")

        st.divider()

        # Case selection
        st.markdown("### 📂 Evaluation Test Case")
        cases = load_cases()
        if not cases:
            st.error("No evaluations found. Please ensure sessions.json is present.")
            return
        labels = [format_case(c) for c in cases]
        chosen = st.selectbox("Select Case", labels)
        case = cases[labels.index(chosen)]

        st.divider()
        st.caption("VinUni Lab 17 — Advanced AI Agents")

    # Main dashboard tabs
    tab_playground, tab_metrics = st.tabs(["💬 Chat Playground", "📊 System Benchmark"])

    # ------------------ PLAYGROUND TAB ------------------
    with tab_playground:
        # Check if case is a golden case or practice case
        is_golden_case = case.get("id", "").startswith("G")
        case_type_badge = "🏆 GOLDEN CASE" if is_golden_case else "📝 PRACTICE CASE"
        case_badge_color = "#d97706" if is_golden_case else "#2563eb"
        
        st.markdown(
            f'<div class="lab-card">'
            f'<span class="lab-badge" style="background:{case_badge_color}">{case_type_badge}</span>'
            f'{layer_badge(case.get("expected_layer","?"))}'
            f'<b>{case["id"]}</b><br>'
            f'<span class="lab-kv"><b>User:</b> <code>{case.get("user_id","-")}</code> &nbsp;·&nbsp; '
            f'<b>Thread:</b> <code>{case.get("thread_id","-")}</code></span>'
            f'<p style="margin:.5rem 0; font-size:1.05rem; font-weight:500;">Query: "{case.get("query","")}"</p>'
            f'<span class="lab-kv" style="opacity: 0.7;">{case.get("description","")}</span></div>',
            unsafe_allow_html=True,
        )

        if st.session_state.get("case_id") != case["id"]:
            st.session_state.case_id = case["id"]
            st.session_state.chat = []
            st.session_state.pop("last_result", None)

        col_run, _ = st.columns([2, 3])
        if col_run.button("▶️ Retrieve Memory for Case", use_container_width=True):
            try:
                memory = StudentMemory(get_zep_client())
                st.session_state.last_result = retrieve_for_case(memory, case, st.session_state.chat, force_all)
            except Exception as exc:
                st.exception(exc)

        result = st.session_state.get("last_result")
        if result:
            st.markdown("### 🔎 Retrieved Context Logs")
            active = [k for k, v in result["layers"].items() if v.strip()]
            st.markdown(" ".join(layer_badge(k) for k in active) or "*(nothing retrieved)*", unsafe_allow_html=True)

            if result.get("budget"):
                cols = st.columns(4)
                for i, layer in enumerate(("short_term", "long_term", "episodic", "semantic")):
                    b = result["budget"].get(layer, {})
                    cols[i].metric(
                        layer,
                        f"{b.get('used_tokens', 0)} tok",
                        help=f"limit {b.get('limit_tokens', 0)} · raw {b.get('raw_tokens', 0)}",
                    )

            # Processing & Execution logs
            if result.get("logs"):
                with st.expander("🖥️ Processing & Execution Trace", expanded=True):
                    st.code("\n".join(result["logs"]), language="log")

            # Fact & Evidence Auditor
            with st.expander("📝 Recalled Facts & Evidence Audit", expanded=True):
                # Check for facts inside each layer and display in custom formatted divs
                for layer, content in result["layers"].items():
                    if content.strip():
                        # Determine label
                        if layer == "short_term":
                            lbl, icon = "Short-term Cache & Session Notes", "💬"
                        elif layer == "long_term":
                            lbl, icon = "Long-Term Memory Facts", "📌"
                        elif layer == "episodic":
                            lbl, icon = "Episodic Trace", "🎬"
                        else:
                            lbl, icon = "Semantic Knowledge Graph Fact", "📖"
                        
                        st.markdown(f'<div class="fact-title">{icon} {lbl}</div>', unsafe_allow_html=True)
                        st.markdown(f'<div class="fact-container {layer}-border">{content}</div>', unsafe_allow_html=True)

            with st.expander("📜 Merged Context Block (Trimmed for Budget)", expanded=False):
                st.code(result.get("merged_context") or "(empty)", language="markdown")

        # Chat interface
        st.divider()
        st.markdown("### 💬 Conversation Thread")

        # Draw existing chat history
        for msg in st.session_state.get("chat", []):
            if msg["role"] == "user":
                with st.chat_message("user"):
                    st.write(msg["content"])
            else:
                # Assistant message (potentially side-by-side)
                with st.chat_message("assistant"):
                    if comp_mode and "content_no_memory" in msg:
                        c1, c2 = st.columns(2)
                        with c1:
                            st.markdown('<div class="chat-col-title chat-mem-title">🧠 With Memory (Grounded)</div>', unsafe_allow_html=True)
                            st.markdown(f'<div class="chat-response-card">{msg["content"]}</div>', unsafe_allow_html=True)
                        with c2:
                            st.markdown('<div class="chat-col-title chat-nomem-title">❌ No Memory (Baseline)</div>', unsafe_allow_html=True)
                            st.markdown(f'<div class="chat-response-card">{msg["content_no_memory"]}</div>', unsafe_allow_html=True)
                    else:
                        st.write(msg["content"])

                    # Show processing trace for this turn if available
                    if msg.get("logs"):
                        with st.expander("🖥️ Turn Processing Trace", expanded=False):
                            st.code("\n".join(msg["logs"]), language="log")

                    # If this message has audit facts, show them
                    if msg.get("facts"):
                        with st.expander("📍 Recalled Facts in this turn", expanded=False):
                            for l, text in msg["facts"].items():
                                if text.strip():
                                    st.markdown(f"**{l.upper()}:** {text}")

        # Input text box
        prompt = st.chat_input("Ask something as this user…")
        if prompt:
            st.session_state.chat.append({"role": "user", "content": prompt})
            with st.chat_message("user"):
                st.write(prompt)
            
            try:
                memory = StudentMemory(get_zep_client())
                # Retrieve context with memory (using current settings)
                follow = retrieve_for_case(memory, {**case, "query": prompt}, st.session_state.chat, force_all)
                st.session_state.last_result = follow
                context = follow.get("merged_context", "")

                with st.chat_message("assistant"):
                    reply_with_mem = ""
                    reply_no_mem = ""
                    
                    if gemini_available():
                        # 1. Generate memory-grounded reply
                        with st.spinner("Thinking (with memory)..."):
                            reply_with_mem = generate_reply(
                                context, 
                                st.session_state.chat[:-1], 
                                prompt, 
                                model=selected_model
                            )
                        
                        # 2. Generate no-memory baseline reply
                        if comp_mode:
                            with st.spinner("Thinking (no memory)..."):
                                reply_no_mem = generate_reply(
                                    "", 
                                    st.session_state.chat[:-1], 
                                    prompt, 
                                    model=selected_model
                                )
                    else:
                        reply_with_mem = f"⚠️ API Key missing. Retrieved context:\n\n{context}"
                        reply_no_mem = "⚠️ API Key missing. No memory context provided."

                    # Display replies
                    if comp_mode:
                        c1, c2 = st.columns(2)
                        with c1:
                            st.markdown('<div class="chat-col-title chat-mem-title">🧠 With Memory (Grounded)</div>', unsafe_allow_html=True)
                            st.markdown(f'<div class="chat-response-card">{reply_with_mem}</div>', unsafe_allow_html=True)
                        with c2:
                            st.markdown('<div class="chat-col-title chat-nomem-title">❌ No Memory (Baseline)</div>', unsafe_allow_html=True)
                            st.markdown(f'<div class="chat-response-card">{reply_no_mem}</div>', unsafe_allow_html=True)
                    else:
                        st.write(reply_with_mem)

                    # Show processing trace for this turn
                    if follow.get("logs"):
                        with st.expander("🖥️ Turn Processing Trace", expanded=False):
                            st.code("\n".join(follow["logs"]), language="log")

                    # Save turns
                    chat_turn = {
                        "role": "assistant",
                        "content": reply_with_mem,
                        "facts": {k: v for k, v in follow["layers"].items() if v.strip()},
                        "logs": follow.get("logs")
                    }
                    if comp_mode:
                        chat_turn["content_no_memory"] = reply_no_mem
                    
                    st.session_state.chat.append(chat_turn)

                    # Display facts used in an expander below
                    active_layers = {k: v for k, v in follow["layers"].items() if v.strip()}
                    if active_layers:
                        with st.expander("📍 Recalled Facts in this turn", expanded=False):
                            for l, text in active_layers.items():
                                st.markdown(f"**{l.upper()}:** {text}")
            except Exception as exc:
                st.exception(exc)


    # ------------------ METRICS / BENCHMARK TAB ------------------
    with tab_metrics:
        st.markdown("## 📊 System Benchmarking & Proof")
        st.caption("Compare retrieval performance, hit rate and token reduction between memory-enabled and no-memory configurations.")

        # Sub tabs for Practice and Golden benchmarks
        bench_tab_practice, bench_tab_golden = st.tabs(["📝 Practice Set (11 Cases)", "🏆 Golden Set (20 Cases)"])

        # Practice benchmark view
        with bench_tab_practice:
            st.markdown("### 📝 Practice Evaluation Set Analysis")
            
            # Re-run benchmark button
            if st.button("⚡ Run Practice Benchmark Suite", key="run_practice_btn"):
                with st.spinner("Executing practice benchmark evaluations... This may take up to 20 seconds."):
                    success = run_benchmark_suite()
                    if success:
                        st.success("Benchmark completed! Reloading metrics...")
                        st.rerun()

            comp_data = get_benchmark_comparison_data()
            if not comp_data:
                st.warning("No benchmark results found. Click the button above to execute the benchmark suite and generate the reports.")
            else:
                m_sum = comp_data["memory"]
                b_sum = comp_data["no_memory"]

                # Visual stats
                m1, m2, m3 = st.columns(3)
                with m1:
                    st.metric(
                        label="🧠 Evidence Hit Rate (Memory)",
                        value=f"{m_sum.get('memory_hit_rate', 0.0) * 100:.1f}%",
                        delta=f"{(m_sum.get('memory_hit_rate', 0.0) - b_sum.get('memory_hit_rate', 0.0)) * 100:+.1f}% vs baseline",
                        help="Retrieval accuracy of required ground truth markers."
                    )
                with m2:
                    st.metric(
                        label="✅ Passed Evaluation Cases",
                        value=f"{m_sum.get('passed', 0)} / {m_sum.get('cases', 0)}",
                        delta=f"+{m_sum.get('passed', 0) - b_sum.get('passed', 0)} cases vs baseline"
                    )
                with m3:
                    st.metric(
                        label="⚡ Avg Latency (Memory)",
                        value=f"{m_sum.get('avg_latency_ms', 0.0):.1f} ms",
                        delta=f"{m_sum.get('avg_latency_ms', 0.0) - b_sum.get('avg_latency_ms', 0.0):+.1f} ms vs baseline",
                        delta_color="inverse"
                    )

                # Chart comparison
                st.markdown("#### 📈 Visual Comparison")
                chart_df = pd.DataFrame({
                    "Metric": ["Hit Rate (%)", "Passed Cases", "Token Reduction (%)"],
                    "Memory Agent": [
                        m_sum.get("memory_hit_rate", 0.0) * 100, 
                        m_sum.get("passed", 0), 
                        m_sum.get("avg_token_reduction", 0.0) * 100
                    ],
                    "No-Memory Baseline": [
                        b_sum.get("memory_hit_rate", 0.0) * 100, 
                        b_sum.get("passed", 0), 
                        b_sum.get("avg_token_reduction", 0.0) * 100
                    ]
                })
                st.dataframe(chart_df, use_container_width=True, hide_index=True)

                # Case comparison details
                st.markdown("#### 📋 Practice Case Results Details")
                
                records = []
                mem_cases = {c["id"]: c for c in comp_data["memory_cases"]}
                no_mem_cases = {c["id"]: c for c in comp_data["no_memory_cases"]}

                # Only practice cases
                practice_cases = [c for c in cases if not c["id"].startswith("G")]

                for c in practice_cases:
                    cid = c["id"]
                    mc = mem_cases.get(cid, {})
                    nmc = no_mem_cases.get(cid, {})
                    records.append({
                        "Case ID": cid,
                        "Layer": c.get("expected_layer"),
                        "Query": c.get("query"),
                        "Ground Truth Evidence": ", ".join(c.get("must_contain_all", [])),
                        "Memory Agent Status": "🟢 PASS" if mc.get("passed") else "🔴 FAIL",
                        "No-Memory Status": "🟢 PASS" if nmc.get("passed") else "🔴 FAIL",
                    })
                
                st.dataframe(pd.DataFrame(records), use_container_width=True, hide_index=True)

        # Golden benchmark view
        with bench_tab_golden:
            st.markdown("### 🏆 Golden Set Performance Analysis")
            
            # Re-run button for golden benchmark
            if st.button("⚡ Run Golden Benchmark Suite", key="run_golden_btn"):
                with st.spinner("Executing golden benchmark evaluations... This may take up to 25 seconds."):
                    success = run_golden_benchmark_suite()
                    if success:
                        st.success("Golden benchmark completed!")
                        st.rerun()

            golden_data = get_golden_benchmark_data()
            if not golden_data:
                st.warning("No golden benchmark results found. If data/golden_eval.json exists, click the button above to run the golden benchmark and compile the results.")
            else:
                g_sum = golden_data.get("summary", {})
                g_results = golden_data.get("results", [])

                # Metrics cards
                c1, c2, c3, c4 = st.columns(4)
                with c1:
                    st.metric(
                        label="🎯 Evidence Hit Rate",
                        value=f"{g_sum.get('memory_hit_rate', 0.0) * 100:.1f}%",
                        help="Retrieval accuracy on hidden evaluations."
                    )
                with c2:
                    st.metric(
                        label="✅ Passed Golden Cases",
                        value=f"{g_sum.get('passed', 0)} / {g_sum.get('cases', 0)}",
                    )
                with c3:
                    st.metric(
                        label="⚡ Avg Latency",
                        value=f"{g_sum.get('avg_latency_ms', 0.0):.1f} ms",
                    )
                with c4:
                    is_perfect = g_sum.get("perfect", False)
                    points = g_sum.get("golden_points", 0)
                    st.metric(
                        label="🎁 Golden Bonus Points",
                        value=f"+{points} / 10",
                        delta="100% Required" if not is_perfect else "Perfect Run!",
                        delta_color="normal" if is_perfect else "inverse"
                    )

                # Explanatory card
                if is_perfect:
                    st.success("🎉 **Perfect Golden Run (20/20 Cases Passed)!** The student memory implementation retrieves evidence with 100% precision. Golden bonus is awarded!")
                else:
                    st.warning("⚠️ **Golden set not perfect yet.** You must pass all 20 cases to secure the 10-point bonus.")

                # Golden Case Table
                st.markdown("#### 📋 Golden Case Results Details")
                records = []
                
                try:
                    g_evals = {c["id"]: c for c in load_golden_evaluations()}
                except Exception:
                    g_evals = {}

                for r in g_results:
                    cid = r["id"]
                    orig = g_evals.get(cid, {})
                    records.append({
                        "Case ID": cid,
                        "Layer": r.get("layer"),
                        "Query": r.get("query"),
                        "Required Ground Truth": ", ".join(orig.get("must_contain_all", [])) if orig else "-",
                        "Status": "🟢 PASS" if r.get("passed") else "🔴 FAIL",
                        "Latency (ms)": r.get("latency_ms"),
                        "Tokens": r.get("retrieved_tokens"),
                        "Reduction": f"{r.get('token_reduction', 0.0)*100:.1f}%",
                        "Error/Missing": r.get("error") or (", ".join(r.get("missing", [])) if r.get("missing") else "")
                    })

                st.dataframe(pd.DataFrame(records), use_container_width=True, hide_index=True)

        # Common explanation
        st.info(
            "💡 **How to interpret token reduction:** A no-memory baseline may show higher token reduction "
            "simply because it retrieves nothing (100% reduction). Hit rate represents the correct evidence retrieved; "
            "dropping all context is cheap but incorrect."
        )


if __name__ == "__main__":
    main()

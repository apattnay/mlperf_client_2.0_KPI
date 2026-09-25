"""Generates the prompt/asset content + preset config JSON for each roofline-calibration preset
(see presets.py) under data/prompts/llama_3_1_8b_instruct/roofline_calibration/ and
data/configs/kpi_presets/roofline_calibration/ - deterministic filler text, so results are exactly
reproducible run to run (only the input *shape*, not real task content, matters for calibration).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROMPTS_DIR = REPO_ROOT / "data" / "prompts" / "llama_3_1_8b_instruct" / "roofline_calibration"
CONFIGS_DIR = REPO_ROOT / "data" / "configs" / "kpi_presets" / "roofline_calibration"

_MODEL_URLS = {
    "NPU": {
        "FilePath": "https://client.mlcommons-storage.org/deps/2.0/scenario_files/models/llama3/8b-instruct/NativeOpenVINO/NPU/openvino_model.xml",
        "DataFilePath": "https://client.mlcommons-storage.org/deps/2.0/scenario_files/models/llama3/8b-instruct/NativeOpenVINO/NPU/openvino_model.bin",
        "TokenizerPath": "https://client.mlcommons-storage.org/deps/2.0/scenario_files/models/llama3/8b-instruct/NativeOpenVINO/NPU/IHV-OV-tokenizers.zip",
    },
    "GPU": {
        "FilePath": "https://client.mlcommons-storage.org/deps/2.0/scenario_files/models/llama3/8b-instruct/NativeOpenVINO/GPU/openvino_model.xml",
        "DataFilePath": "https://client.mlcommons-storage.org/deps/2.0/scenario_files/models/llama3/8b-instruct/NativeOpenVINO/GPU/openvino_model.bin",
        "TokenizerPath": "https://client.mlcommons-storage.org/deps/2.0/scenario_files/models/llama3/8b-instruct/NativeOpenVINO/GPU/IHV-OV-tokenizers.zip",
    },
}

_DEFAULT_SEARCH = {
    "method": "greedy", "temperature": 0.6, "top_k": 1, "top_p": 0.9, "stop_on_eos": True,
}

_FILLER_SENTENCES = [
    "The build pipeline recompiles the affected translation units before re-running the regression suite.",
    "Memory bandwidth utilization during the decode phase depends heavily on batch size and KV-cache depth.",
    "The scheduler partitions independent tasks across available worker threads to reduce end-to-end latency.",
    "Static analysis flags unchecked buffer accesses that could lead to out-of-bounds reads during parsing.",
    "The configuration loader validates every field against the JSON schema before the executor starts.",
    "Profiling shows the hot path spends most of its time waiting on memory rather than executing instructions.",
    "The test harness replays recorded fixtures to keep the benchmark deterministic across repeated runs.",
    "Each worker checkpoints its partial state so a restart can resume without redoing completed work.",
    "The linker resolves symbols lazily, which shifts some of the startup cost into the first function call.",
    "Cache locality matters more than raw clock speed once the working set exceeds the last-level cache size.",
]


def _filler_text(target_tokens: int, tokens_per_word: float = 1.3) -> str:
    """Deterministic filler text sized to approximately `target_tokens` (English text averages
    ~1.3 tokens/word for this tokenizer family - approximate on purpose, since a calibration
    SWEEP only needs correctly *ordered*, roughly-known shapes, not exact token counts)."""
    target_words = max(1, int(target_tokens / tokens_per_word))
    words: List[str] = []
    i = 0
    while len(words) < target_words:
        words.extend(_FILLER_SENTENCES[i % len(_FILLER_SENTENCES)].split())
        i += 1
    return " ".join(words[:target_words])


def _write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _base_scenario(device: str, name: str, input_file_path: dict, iterations: int,
                    assets_path: List[str] = None, is_agentic: bool = False,
                    tools_execution: bool = False) -> dict:
    return {
        "SystemConfig": {"Comment": f"Roofline calibration: {name} ({device})", "TempPath": "", "EPDependenciesConfigPath": ""},
        "Scenarios": [
            {
                "Name": "Llama3",
                "Models": [{"ModelName": "Llama-3.1-8B-Instruct_ov-int4-CHw" if device == "NPU" else "Llama-3.1-8B-Instruct_ov-int4-GRw", **_MODEL_URLS[device]}],
                "InputFilePath": input_file_path,
                "AssetsPath": assets_path or [],
                "ResultsVerificationFile": "https://client.mlcommons-storage.org/deps/2.0/scenario_files/llm/generation-greedy-results.json",
                "DataVerificationFile": "",
                "Iterations": iterations,
                "Delay": 5,
                "ExecutionProviders": [{"Name": "NativeOpenVINO", "Config": {"device_type": device}}],
                **({"IsAgentic": True} if is_agentic else {}),
                **({"ToolsExecution": True} if tools_execution else {}),
            }
        ],
    }


def generate_prefill_sweep(device: str, targets: List[int] = (128, 512, 2048, 8192)) -> Path:
    """Preset 1: fixed short output, sweeping input context length - isolates prefill scaling."""
    prompt_paths = []
    for tokens in targets:
        prompt = {
            "model_config": {"context_length": max(tokens * 2, 4096), "search": {**_DEFAULT_SEARCH, "max_length": 32}},
            "apply_chat_template": True,
            "prompts": [{"system": "You are a precise, terse code reviewer.",
                         "user": _filler_text(tokens) + " Summarize the above in exactly one sentence."}],
            "category": f"Roofline Calib Prefill x{tokens}",
        }
        path = PROMPTS_DIR / "prefill_sweep" / f"prefill_{tokens}.json"
        _write_json(path, prompt)
        prompt_paths.append(f"file://{path.as_posix()}")

    config = _base_scenario(device, "prefill_sweep", {"base": prompt_paths}, iterations=3)
    config_path = CONFIGS_DIR / f"prefill_sweep_{device}.json"
    _write_json(config_path, config)
    return config_path


def generate_thin_serving(device: str, iterations: int = 20) -> Path:
    """Preset 3: minimal round trips repeated many times - isolates fixed per-request overhead."""
    prompt = {
        "model_config": {"context_length": 512, "search": {**_DEFAULT_SEARCH, "max_length": 4}},
        "apply_chat_template": True,
        "prompts": [{"system": "Reply with exactly one word.", "user": "Say OK."}],
        "category": "Roofline Calib Thin Serving",
    }
    path = PROMPTS_DIR / "thin_serving" / "thin_serving.json"
    _write_json(path, prompt)

    config = _base_scenario(device, "thin_serving", {"base": [f"file://{path.as_posix()}"]}, iterations=iterations)
    config_path = CONFIGS_DIR / f"thin_serving_{device}.json"
    _write_json(config_path, config)
    return config_path


def generate_kv_cache_growth(device: str, n_turns: int = 4, per_turn_tokens: int = 600) -> Path:
    """Preset 2 (agentic, draft): each scripted 'agent' turn adds ~per_turn_tokens to history -
    schema matches data/prompts/llama_3_1_8b_instruct/swe_agent/swe-agent-prompts.json exactly."""
    d = PROMPTS_DIR / "kv_cache_growth"
    _write_text(d / "calib_warmup.md", "Warm-up: respond with a single short acknowledgement.")
    _write_text(d / "calib_system.md", "You are an assistant reviewing a series of short technical notes.")

    prompts_list = [{"system": "calib_warmup.md"}]
    for turn in range(n_turns):
        user_name = f"calib_user_{turn}.md"
        _write_text(d / user_name, f"Note {turn}: " + _filler_text(80) + " Acknowledge briefly.")
        if turn == 0:
            prompts_list.append({"system": "calib_system.md", "user": user_name})
        else:
            prompts_list.append({"user": user_name})
        if turn < n_turns - 1:  # last turn has no scripted reply to inject before ending
            agent_name = f"calib_agent_{turn}.md"
            _write_text(d / agent_name, _filler_text(per_turn_tokens))
            prompts_list.append({"agent": agent_name})

    scenario = {
        "model_config": {"context_length": max(4096, per_turn_tokens * n_turns * 2), "search": {**_DEFAULT_SEARCH, "max_length": 64}},
        "prompt_files": True,
        "apply_chat_template": True,
        "prompts": prompts_list,
        "category": "Roofline Calib KV Cache Growth",
    }
    scenario_path = d / "calib-kv-cache-prompts.json"
    _write_json(scenario_path, scenario)

    config = _base_scenario(device, "kv_cache_growth", {"base": [f"file://{scenario_path.as_posix()}"]},
                             iterations=1, is_agentic=True)
    config_path = CONFIGS_DIR / f"kv_cache_growth_{device}.json"
    _write_json(config_path, config)
    return config_path


def generate_tool_exec_only(device: str) -> Path:
    """Preset 4 (agentic, draft): prompts the model to invoke tools_sandbox/ scripts directly
    with minimal reasoning in between - see presets.py's confidence="draft" caveat."""
    d = PROMPTS_DIR / "tool_exec_only"
    sandbox_dir = REPO_ROOT / "data" / "prompts" / "llama_3_1_8b_instruct" / "tools_sandbox"
    sandbox_scripts = ["01_profile_csv.py", "02_channel_mix.py", "03_supplier_concentration.py"]

    _write_text(d / "calib_te_warmup.md", "Warm-up: respond with a single short acknowledgement.")
    _write_text(d / "calib_te_system.md",
                "You are an assistant with access to a Python execution tool. When asked to run a "
                "script, call the tool directly with minimal explanation - do not restate the task.")

    prompts_list = [{"system": "calib_te_warmup.md"}]
    for i, script in enumerate(sandbox_scripts):
        user_name = f"calib_te_user_{i}.md"
        _write_text(d / user_name, f"Run `{script}` on `Warehouse_and_Retail_Sales.csv` and report only the exit status.")
        if i == 0:
            prompts_list.append({"system": "calib_te_system.md", "user": user_name})
        else:
            prompts_list.append({"user": user_name})
        if i < len(sandbox_scripts) - 1:
            agent_name = f"calib_te_agent_{i}.md"
            _write_text(d / agent_name, "Acknowledged, proceeding to the next script.")
            prompts_list.append({"agent": agent_name})

    scenario = {
        "model_config": {"context_length": 8192, "search": {**_DEFAULT_SEARCH, "max_length": 256}},
        "prompt_files": True,
        "apply_chat_template": True,
        "prompts": prompts_list,
        "category": "Roofline Calib Tool Exec Only",
    }
    scenario_path = d / "calib-tool-exec-prompts.json"
    _write_json(scenario_path, scenario)

    assets = [f"file://{(sandbox_dir / f).as_posix()}" for f in sandbox_scripts + ["beverage_lib.py", "Warehouse_and_Retail_Sales.csv"]]
    config = _base_scenario(device, "tool_exec_only", {"base": [f"file://{scenario_path.as_posix()}"]},
                             iterations=1, assets_path=assets, is_agentic=True, tools_execution=True)
    config_path = CONFIGS_DIR / f"tool_exec_only_{device}.json"
    _write_json(config_path, config)
    return config_path


_GENERATORS = {
    "prefill_sweep": generate_prefill_sweep,
    "thin_serving": generate_thin_serving,
    "kv_cache_growth": generate_kv_cache_growth,
    "tool_exec_only": generate_tool_exec_only,
}


def generate(preset_key: str, device: str) -> Path:
    return _GENERATORS[preset_key](device)

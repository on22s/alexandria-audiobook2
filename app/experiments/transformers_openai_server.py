#!/usr/bin/env python3
"""Serve an HF checkpoint (optionally + a PEFT adapter) as an OpenAI-compatible
chat endpoint, so models with no GGUF can be measured by the SAME harness.

Why this exists: Gemma 4 E2B and E4B have no llama.cpp GGUF, so adapters
trained for them had no serving path and could not be evaluated at all. The
alternative was a second evaluation script, which would have been a second
instrument - every number in this project comes from lora_serving_eval.py, and
two independently-maintained scorers drift. This serves the model instead, and
the existing harness, scoring, provenance and artifact format are untouched.

It refuses rather than pretends:

- `response_format` (the harness's --structured-output auto) is REFUSED with a
  400. llama.cpp constrains generation with a grammar; this does not. Silently
  ignoring it would let an artifact record `structured_output: auto` for a run
  that had none, so the caller must pass --structured-output off and the
  artifact then says so.
- Any sampling parameter it cannot honour is refused, not quietly dropped.

Greedy only: temperature must be 0 (or absent), which is what every row in the
ladder uses.
"""
import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {}


def _load(args):
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True)
    if args.chat_template:
        template = open(args.chat_template, encoding="utf-8").read()
        if not template.strip():
            raise ValueError(f"chat template is empty: {args.chat_template}")
        tok.chat_template = template
    if not tok.chat_template:
        raise ValueError(
            f"{args.model} has no chat template and --chat-template was not given; "
            "the prompt shape would not match what the model was trained on")
    model = AutoModelForCausalLM.from_pretrained(
        args.model, dtype=torch.bfloat16, device_map="auto", trust_remote_code=True)
    if args.adapter:
        from peft import PeftModel
        model = PeftModel.from_pretrained(model, args.adapter)
        model = model.merge_and_unload()
    model.eval()
    STATE.update(tok=tok, model=model, name=args.served_name,
                 max_new=args.max_new_tokens, model_path=args.model,
                 n_ctx=args.n_ctx,
                 ftype=("bf16+adapter" if args.adapter else "bf16"))


def _generate(messages, max_tokens):
    import torch
    tok, model = STATE["tok"], STATE["model"]
    prompt = tok.apply_chat_template(messages, tokenize=False,
                                     add_generation_prompt=True)
    enc = tok(prompt, return_tensors="pt", add_special_tokens=False).to(model.device)
    with torch.no_grad():
        out = model.generate(**enc, do_sample=False,
                             max_new_tokens=max_tokens or STATE["max_new"],
                             pad_token_id=tok.pad_token_id or tok.eos_token_id)
    text = tok.decode(out[0][enc["input_ids"].shape[1]:], skip_special_tokens=True)
    return text, int(enc["input_ids"].shape[1]), int(out.shape[1] - enc["input_ids"].shape[1])


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):  # the eval log is the record, not stderr noise
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", "/v1/health"):
            self._send(200, {"status": "ok"})
        elif self.path.rstrip("/") == "/props":
            # The harness records serving provenance from /props before it will
            # score anything (manifest.lmstudio_state). Same shape llama.cpp
            # uses, plus server_runtime so the artifact does not claim to be
            # llama.cpp when it is not.
            self._send(200, {
                "default_generation_settings": {
                    "n_ctx": STATE.get("n_ctx"),
                    "params": {"reasoning_format": None},
                },
                "total_slots": 1,
                "model_alias": STATE.get("name"),
                "build_info": "transformers_openai_server",
                "model_path": STATE.get("model_path"),
                "model_ftype": STATE.get("ftype"),
                "server_runtime": "transformers",
            })
        elif self.path.rstrip("/") == "/v1/models":
            self._send(200, {"object": "list",
                             "data": [{"id": STATE.get("name", "local"),
                                       "object": "model"}]})
        else:
            self._send(404, {"error": {"message": f"no route {self.path}"}})

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._send(404, {"error": {"message": f"no route {self.path}"}})
            return
        try:
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"] or 0)) or b"{}")
        except ValueError as exc:
            self._send(400, {"error": {"message": f"bad JSON: {exc}"}})
            return

        if req.get("response_format"):
            self._send(400, {"error": {"message":
                "this server does not constrain generation, so it will not accept "
                "response_format: re-run with --structured-output off so the "
                "artifact records the absence of a schema rather than claiming one"}})
            return
        temp = req.get("temperature")
        if temp not in (None, 0, 0.0):
            self._send(400, {"error": {"message":
                f"greedy only; temperature={temp} was requested and will not be honoured"}})
            return

        messages = req.get("messages") or []
        if STATE.get("stub"):
            text, pt, ct = '[{"n": 0, "speaker": "NARRATOR"}]', 0, 0
        else:
            try:
                text, pt, ct = _generate(messages, req.get("max_tokens"))
            except Exception as exc:  # a failure must reach the caller, not a 200
                self._send(500, {"error": {"message": f"{type(exc).__name__}: {exc}"}})
                return
        self._send(200, {
            "id": "chatcmpl-local", "object": "chat.completion",
            "created": int(time.time()), "model": STATE.get("name", "local"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": pt, "completion_tokens": ct,
                      "total_tokens": pt + ct}})


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--model", default=None)
    ap.add_argument("--adapter", default=None,
                    help="PEFT adapter directory; merged into the base at load")
    ap.add_argument("--chat-template", default=None)
    ap.add_argument("--served-name", default="local-transformers",
                    help="the model name the endpoint reports and the harness sends")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8175)
    ap.add_argument("--max-new-tokens", type=int, default=4096)
    ap.add_argument("--n-ctx", type=int, default=32768,
                    help="context length reported to the harness as provenance; "
                         "match what the model is actually being given")
    ap.add_argument("--stub", action="store_true",
                    help="serve a canned reply without loading a model, to check "
                         "the HTTP contract against the harness on a machine with "
                         "no GPU")
    args = ap.parse_args()
    if args.stub:
        STATE.update(stub=True, name=args.served_name, max_new=args.max_new_tokens,
                     model_path="(stub)", n_ctx=args.n_ctx, ftype="(stub)")
    else:
        if not args.model:
            ap.error("--model is required unless --stub is given")
        _load(args)
    print(f"serving {args.served_name} on http://{args.host}:{args.port}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()

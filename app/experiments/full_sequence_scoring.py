"""Full-name candidate likelihoods from llama.cpp's completion endpoint.

llama.cpp does not expose prompt-token likelihoods.  This scorer forces one
candidate token at a time with a known logit bias, then algebraically removes
that bias.  It therefore compares complete names, including shared honorifics.
"""
import math


def recover_logprob(biased_logprob, bias):
    """Return the original log probability before one-token logit bias."""
    biased_probability = math.exp(biased_logprob)
    if not 0.0 < biased_probability < 1.0:
        raise ValueError("biased probability is numerically unrecoverable")
    first = bias + math.log1p(-biased_probability)
    second = biased_logprob
    maximum = max(first, second)
    log_denominator = maximum + math.log(
        math.exp(first - maximum) + math.exp(second - maximum))
    return biased_logprob - log_denominator


class LlamaSequenceScorer:
    """Score candidate strings using an already-running llama.cpp server."""

    def __init__(self, session, base_url, model="qwen3-14b",
                 biases=(20.0, 25.0, 30.0)):
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.biases = tuple(biases)

    def _post(self, path, body):
        response = self.session.post(self.base_url + path, json=body, timeout=120)
        response.raise_for_status()
        return response.json()

    def render_chat(self, user_prompt):
        result = self._post("/apply-template", {
            "messages": [{"role": "user", "content": user_prompt}],
            "add_generation_prompt": True,
        })
        return result["prompt"]

    def tokenize(self, text, add_special=False):
        return self._post("/tokenize", {
            "content": text, "add_special": add_special,
        })["tokens"]

    def score(self, rendered_prompt, candidate):
        """Return summed and mean log likelihood for one complete candidate."""
        prompt_tokens = self.tokenize(rendered_prompt, add_special=True)
        candidate_tokens = self.tokenize(" " + candidate.strip(), add_special=False)
        if not candidate_tokens:
            raise ValueError("candidate must contain at least one token")
        token_logprobs = []
        for token in candidate_tokens:
            recovered = None
            for bias in self.biases:
                result = self._post("/v1/completions", {
                    "model": self.model, "prompt": prompt_tokens,
                    "max_tokens": 1, "temperature": 1.0,
                    "top_k": 0, "top_p": 1.0, "min_p": 0.0,
                    "logprobs": 1, "seed": 1,
                    "logit_bias": {str(token): bias},
                })
                content = result["choices"][0]["logprobs"]["content"][0]
                if content["id"] == token and content["logprob"] < -1e-7:
                    recovered = recover_logprob(content["logprob"], bias)
                    break
            if recovered is None:
                raise RuntimeError("no bias level forced a non-saturated token")
            token_logprobs.append(recovered)
            prompt_tokens.append(token)
        total = sum(token_logprobs)
        return {"sum_logprob": total,
                "mean_logprob": total / len(token_logprobs),
                "tokens": len(token_logprobs)}

    def rank(self, user_prompt, candidates, length_normalize=True):
        rendered = self.render_chat(user_prompt)
        scores = []
        field = "mean_logprob" if length_normalize else "sum_logprob"
        for candidate in candidates:
            score = self.score(rendered, candidate)
            scores.append({"candidate": candidate, **score})
        return sorted(scores, key=lambda row: row[field], reverse=True)

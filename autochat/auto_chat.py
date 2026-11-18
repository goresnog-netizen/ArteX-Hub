# auto_chat.py - ArteX-HUB auto-chat orchestrator (no pairing)
# Usage: python auto_chat.py --provider deepseek --prompts prompts.txt --delay 5
import argparse, time, json, urllib.request, urllib.error, sys, os

BRIDGE = "http://127.0.0.1:17614"
PROVIDERS = {
    "deepseek": "https://chat.deepseek.com",
    "chatgpt": "https://chatgpt.com",
    "claude": "https://claude.ai",
    "gemini": "https://gemini.google.com",
    "grok": "https://grok.com",
    "kimi": "https://www.kimi.ai",
    "zai": "https://chat.z.ai",
    "qwen": "https://chat.qwen.ai",
}


def get_token():
    try:
        r = urllib.request.urlopen(f"{BRIDGE}/auth-token", timeout=5)
        d = json.loads(r.read())
        return d.get("token")
    except Exception as e:
        print("token err", e)
        return None


def send_prompt(provider, prompt):
    # placeholder: integrate with extension bridge call_tool
    print(f"[{provider}] sending: {prompt[:60]}...")
    time.sleep(1)
    return {"status": "ok", "response": "placeholder"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="deepseek")
    ap.add_argument("--prompts", default="prompts.txt")
    ap.add_argument("--delay", type=int, default=5)
    args = ap.parse_args()
    prompts = [l.strip() for l in open(args.prompts, encoding="utf-8") if l.strip()]
    print(f"provider={args.provider} prompts={len(prompts)} delay={args.delay}")
    for i, p in enumerate(prompts, 1):
        res = send_prompt(args.provider, p)
        print(f"step {i}/{len(prompts)} -> {res['status']}")
        if i < len(prompts):
            print(f"waiting {args.delay}s...")
            time.sleep(args.delay)
    print("done end-to-end")


if __name__ == "__main__":
    main()

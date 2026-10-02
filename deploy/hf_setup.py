"""Create everything VORA needs on Hugging Face, from your own machine, in one run.

    $env:HF_TOKEN = "hf_..."                       # a WRITE token: huggingface.co -> Settings -> Access Tokens
    .\\.venv\\Scripts\\python deploy\\hf_setup.py --cors https://<your-pages-site>

It (1) finds your username from the token, (2) creates the private backup dataset ``vora-db``, (3) creates the Docker
Space ``vora-api`` (CPU basic, public), (4) copies your model keys from .env into the Space as secrets, plus the backup
token and a generated SearXNG secret, (5) sets the Space variables, and (6) pushes the code to the Space.

The token is read from the environment only; it is never written to a file or printed. Re-running is safe: existing
repos are kept and values are overwritten.
"""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRET_KEYS = ("GROQ_API_KEY", "GEMINI_API_KEY", "NVIDIA_API_KEY")


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--space", default="vora-api", help="Space name (default vora-api)")
    parser.add_argument("--dataset", default="vora-db", help="backup dataset name (default vora-db)")
    parser.add_argument("--cors", default="http://localhost:5173",
                        help="web origin(s) allowed to call the API, comma-separated (your Pages address)")
    parser.add_argument("--supabase-url", default="", help="your Supabase project URL (can be added later)")
    parser.add_argument("--no-push", action="store_true", help="set up the repos and settings but do not push the code")
    args = parser.parse_args()

    token = os.getenv("HF_TOKEN", "").strip()
    if not token:
        print("Set HF_TOKEN first:  $env:HF_TOKEN = 'hf_...'   (a Write token)")
        return 2
    from huggingface_hub import HfApi

    api = HfApi(token=token)
    user = api.whoami()["name"]
    print(f"signed in as {user}")

    dataset_id, space_id = f"{user}/{args.dataset}", f"{user}/{args.space}"
    api.create_repo(dataset_id, repo_type="dataset", private=True, exist_ok=True)
    print(f"dataset ready: {dataset_id} (private)")
    api.create_repo(space_id, repo_type="space", space_sdk="docker", private=False, exist_ok=True)
    print(f"space ready:   {space_id}")

    env = read_env(ROOT / ".env")
    secrets_to_set = {key: env[key] for key in SECRET_KEYS if env.get(key)}
    secrets_to_set["HF_TOKEN"] = token
    secrets_to_set["SEARXNG_SECRET"] = env.get("SEARXNG_SECRET") or secrets.token_hex(24)
    if args.supabase_url or env.get("SUPABASE_URL"):
        secrets_to_set["SUPABASE_URL"] = args.supabase_url or env["SUPABASE_URL"]
    for name, value in secrets_to_set.items():
        api.add_space_secret(space_id, name, value)
    print("secrets set:   " + ", ".join(sorted(secrets_to_set)))
    missing = [key for key in SECRET_KEYS if key not in secrets_to_set]
    if missing:
        print("  (no value in .env for: " + ", ".join(missing) + "; add them in the Space settings if you use them)")

    variables = {"VORA_AUTH": "supabase", "VORA_BACKUP_REPO": dataset_id, "VORA_CORS_ORIGINS": args.cors}
    for name, value in variables.items():
        api.add_space_variable(space_id, name, value)
    print("variables set: " + ", ".join(f"{k}={v}" for k, v in variables.items()))

    if args.no_push:
        print("skipped the code push (--no-push)")
    else:
        # The token travels in the push address for this one command only; it is not stored in the git config.
        url = f"https://{user}:{token}@huggingface.co/spaces/{space_id}"
        print("pushing the code to the Space ...")
        result = subprocess.run(["git", "push", url, "HEAD:main", "--force"], cwd=ROOT, capture_output=True, text=True)
        if result.returncode != 0:
            print(result.stderr.replace(token, "***").strip())
            return 1
        print("pushed.")
    host = f"https://{user.lower()}-{args.space.lower()}.hf.space"
    print(f"\nBuild log: https://huggingface.co/spaces/{space_id}  (Logs tab; the first build takes several minutes)")
    print(f"When it says Running, check:  {host}/ready")
    print(f"Frontend variable VITE_API_BASE_URL = {host}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Upload submission to HuggingFace using chunked large folder upload."""

import argparse
import os
import shutil
import tempfile

from huggingface_hub import HfApi


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-id", required=True)
    parser.add_argument("--local-dir", required=True)
    parser.add_argument("--path-in-repo", required=True)
    parser.add_argument("--token", default=None)
    args = parser.parse_args()

    token = args.token or os.environ.get("HF_TOKEN")
    if not token:
        raise SystemExit("Error: provide --token or set HF_TOKEN env var")

    api = HfApi(token=token)

    # Ensure repo exists
    try:
        api.create_repo(args.repo_id, repo_type="dataset", exist_ok=True)
    except Exception as e:
        if "409" in str(e) or "already" in str(e).lower():
            pass
        else:
            raise

    # Build nested directory structure with actual copies (not symlinks)
    tmpdir = tempfile.mkdtemp(prefix="hf-upload-")
    dest = os.path.join(tmpdir, args.path_in_repo)
    print(f"Copying {args.local_dir} -> {dest} ...")
    shutil.copytree(args.local_dir, dest)

    # Count files
    file_count = sum(len(files) for _, _, files in os.walk(dest))
    total_size = sum(
        os.path.getsize(os.path.join(dp, f))
        for dp, _, fns in os.walk(dest)
        for f in fns
    )
    print(f"Files to upload: {file_count} ({total_size / 1024 / 1024:.1f} MB)")

    print(f"Uploading to {args.repo_id} ...")
    print("This may take a long time for large submissions...")

    try:
        api.upload_large_folder(
            repo_id=args.repo_id,
            folder_path=tmpdir,
            repo_type="dataset",
        )
        print("\nUpload complete!")
        print(f"\nNow open a PR at:")
        print(f"  https://huggingface.co/datasets/harborframework/terminal-bench-2-leaderboard/discussions/new")
    finally:
        print(f"Cleaning up temp dir {tmpdir} ...")
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()

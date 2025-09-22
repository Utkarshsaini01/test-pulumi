#!/usr/bin/env python3
"""
scripts/handle_new_apps.py

Usage:
  python scripts/handle_new_apps.py --base-branch main --head-branch feature/pr-branch \
    --deploy-repo owner/deploy --infra-repo owner/infra-repo --addons-repo owner/addons
"""

import argparse
import os
import subprocess
import sys
import tempfile
import yaml
from pathlib import Path
from typing import Dict

import jwt
import time
import requests


GITHUB_API = "https://api.github.com"

def generate_jwt(app_id: str, private_key_path: str) -> str:
    """Generate a JWT for the GitHub App"""
    with open(private_key_path, "r") as f:
        private_key = f.read()

    now = int(time.time())
    payload = {
        "iat": now,
        "exp": now + (10 * 60),  # JWT valid for 10 minutes
        "iss": app_id
    }

    encoded_jwt = jwt.encode(payload, private_key, algorithm="RS256")
    return encoded_jwt

def get_installation_token(jwt_token: str, installation_id: str) -> str:
    """Get a GitHub App installation access token"""
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Accept": "application/vnd.github+json"
    }
    url = f"{GITHUB_API}/app/installations/{installation_id}/access_tokens"
    resp = requests.post(url, headers=headers)
    resp.raise_for_status()
    return resp.json()["token"]


# helpers
def run(cmd, cwd=None, check=True, capture=False, env=None):
    print(f"> {cmd} (cwd={cwd})")
    res = subprocess.run(cmd, shell=True, cwd=cwd, check=False, text=True, capture_output=capture, env=env)
    if check and res.returncode != 0:
        print(res.stdout)
        print(res.stderr, file=sys.stderr)
        raise subprocess.CalledProcessError(res.returncode, cmd)
    return res.stdout if capture else res

def load_apps_from_file(path) -> Dict[str, Dict]:
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    apps = data.get("applications", {})
    if isinstance(apps, list):
        # legacy list format
        return {a["app_name"]: a for a in apps if "app_name" in a}
    elif isinstance(apps, dict):
        # new dict format
        return apps
    else:
        return {}

def apps_by_name(apps: Dict[str, Dict]) -> Dict[str, Dict]:
    return apps  # already keyed by app_name in new format

def git_checkout(ref, cwd=None):
    run(f"git fetch origin {ref} --depth=1", cwd=cwd)
    run(f"git checkout {ref}", cwd=cwd)

def git_commit_push_same_branch(commit_message, actor_name, actor_email, cwd=None, push_ref=None):
    run(f'git config user.name "{actor_name}"', cwd=cwd)
    run(f'git config user.email "{actor_email}"', cwd=cwd)
    run("git add .", cwd=cwd)
    # commit may fail if no changes; handle gracefully
    try:
        run(f'git commit -m "{commit_message}"', cwd=cwd)
    except subprocess.CalledProcessError:
        print("No changes to commit")
    target = push_ref if push_ref else "HEAD:HEAD"
    run(f"git push origin {target}", cwd=cwd)

def create_branch_and_push(branch, cwd):
    run(f"git checkout -b {branch}", cwd=cwd)
    run(f"git push -u origin {branch}", cwd=cwd)

def gh_pr_create(repo, head_branch, base_branch, title, body, gh_token, cwd=None):
    env = os.environ.copy()
    env['GH_TOKEN'] = gh_token
    cmd = f'gh pr create --repo {repo} --head {head_branch} --base {base_branch} --title "{title}" --body "{body}" --web=false'
    out = run(cmd, cwd=cwd, check=True, capture=True, env=env)
    stdout = out if isinstance(out, str) else (out.stdout if hasattr(out,'stdout') else "")
    for line in stdout.splitlines():
        if line.startswith("https://") or line.startswith("http://"):
            return line.strip()
    return stdout.strip()

def gh_pr_comment(repo, pr_number, body, gh_token_env="GITHUB_TOKEN"):
    env = os.environ.copy()
    token = os.environ.get(gh_token_env)
    if not token:
        raise RuntimeError(f"{gh_token_env} is not set in environment")
    env['GH_TOKEN'] = token
    cmd = f'gh pr comment {pr_number} --repo {repo} --body "{body}"'
    run(cmd, env=env)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-branch", required=True)
    parser.add_argument("--head-branch", required=True)
    parser.add_argument("--deploy-repo", required=True, help="owner/deploy")
    parser.add_argument("--infra-repo", required=True, help="owner/infra-repo")
    parser.add_argument("--addons-repo", required=True, help="owner/addons")
    args = parser.parse_args()

    app_id = os.environ.get("APP_ID")
    private_key = os.environ.get("APP_KEY")
    installation_id = os.environ.get("INSTALLATION_ID")
    
    jwt_token = generate_jwt(args.app_id, args.private_key)
    GH_token = get_installation_token(jwt_token, args.installation_id)

    pr_number = os.environ.get("PR_NUMBER") or os.environ.get("GITHUB_REF")
    actor = os.environ.get("GITHUB_ACTOR", "automation-bot")
    actor_email = f"{actor}@users.noreply.github.com"

    repo_root = Path.cwd()
    config_path = repo_root / "config" / "apps.yaml"
    if not config_path.exists():
        print("config/apps.yaml not found in checkout")
        sys.exit(1)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        run(f"git fetch origin {args.base_branch} --depth=1")
        base_file = tmp / "apps_base.yaml"
        try:
            base_content = run(f"git show origin/{args.base_branch}:config/apps.yaml", capture=True)
            with open(base_file, "w") as f:
                f.write(base_content)
        except subprocess.CalledProcessError:
            base_file.write_text("applications: {}\n")

        head_file = repo_root / "config" / "apps.yaml"

        base_map = load_apps_from_file(str(base_file))
        head_map = load_apps_from_file(str(head_file))

        new_app_names = [name for name in head_map.keys() if name not in base_map.keys()]
        if not new_app_names:
            print("No new apps added compared to base branch.")
            return

        print("New apps detected:", new_app_names)

        created_prs = []

        for app_name in new_app_names:
            app = head_map[app_name]
            jira = app.get("jira") or app.get("jira_ticket") or "no-jira"
            envs = app.get("envs", [])
            envs_arg = " ".join(envs) if isinstance(envs, list) else str(envs)
            print(f"Processing app {app_name}, jira={jira}, envs={envs_arg}")

            if (repo_root / "pyproject.toml").exists():
                run("poetry install --no-root", cwd=str(repo_root))
                run(f'poetry run python scripts/new_app.py -a {app_name} -e {envs_arg}', cwd=str(repo_root))
            else:
                run(f'python scripts/new_app.py -a {app_name} -e {envs_arg}', cwd=str(repo_root))

            commit_message = f'chore: update app config for {app_name}'
            run(f'git add .', cwd=str(repo_root))
            try:
                run(f'git commit -m "{commit_message}"', cwd=str(repo_root))
            except subprocess.CalledProcessError:
                print("No changes to commit in deploy repo for this app.")
            head_ref = os.environ.get("PR_HEAD_REF") or args.head_branch
            run(f'git push origin HEAD:{head_ref}', cwd=str(repo_root))
            print("Committed and pushed changes to deploy PR branch.")

            infra_repo_full = args.infra_repo
            tmp_infra = Path(tempfile.mkdtemp())
            token = GH_token
            if not token:
                print("❌ Missing GH_PAT secret, cannot push to infra repo")
                sys.exit(1)

            # Clone infra repo with auth
            run(
                f"git clone https://x-access-token:{token}@github.com/{infra_repo_full}.git {tmp_infra}",
                cwd=None
            )

            # Configure bot identity inside cloned repo
            run('git config user.name "github-actions[bot]"', cwd=tmp_infra)
            run('git config user.email "github-actions[bot]@users.noreply.github.com"', cwd=tmp_infra)

            branch_name = f"new_app/{jira}/configure-{app_name}"
            run(f'git checkout -b {branch_name}', cwd=str(tmp_infra))

            run(f'python scripts/new_app.py -a {app_name} -e {envs_arg}', cwd=str(tmp_infra))

            run('git add .', cwd=str(tmp_infra))
            try:
                run(f'git commit -m "feat: add app {app_name} with envs ({envs_arg})"', cwd=str(tmp_infra))
            except subprocess.CalledProcessError:
                print("No changes to commit in infra repo.")
            run(f'git push -u origin {branch_name}', cwd=str(tmp_infra))
            pr_title = f"Configure {app_name} ({jira})"
            pr_body = f"Automated PR to configure {app_name} for envs: {envs_arg}"
            pr_url = gh_pr_create(infra_repo_full, branch_name, "main", pr_title, pr_body, gh_token=token, cwd=str(tmp_infra))
            created_prs.append((infra_repo_full, pr_url))
            print("Infra PR URL:", pr_url)


            # # ---------- 3) Addons repo - clone, run poetry + script, same branch
            # addons_repo_full = args.addons_repo
            # tmp_addons = Path(tempfile.mkdtemp())
            # try:
            #     run(f'git clone git@github.com:{addons_repo_full}.git {tmp_addons}', cwd=None)
            # except subprocess.CalledProcessError:
            #     run(f'git clone https://github.com/{addons_repo_full}.git {tmp_addons}')
            # run(f'git checkout -b {branch_name}', cwd=str(tmp_addons))

            # # if pyproject present -> use poetry
            # if (tmp_addons / "pyproject.toml").exists():
            #     run("poetry install --no-root", cwd=str(tmp_addons))
            #     run(f'poetry run python scripts/new_app.py -n {app_name} -e {envs_arg}', cwd=str(tmp_addons))
            # else:
            #     run(f'python scripts/new_app.py -n {app_name} -e {envs_arg}', cwd=str(tmp_addons))

            # run('git add .', cwd=str(tmp_addons))
            # try:
            #     run(f'git commit -m "feat(addons): configure {app_name} ({envs_arg})"', cwd=str(tmp_addons))
            # except subprocess.CalledProcessError:
            #     print("No changes to commit in addons repo.")
            # run(f'git push -u origin {branch_name}', cwd=str(tmp_addons))
            # pr_title = f"Addons: configure {app_name} ({jira})"
            # pr_body = f"Automated PR to configure addons for {app_name}. See deploy PR #{pr_number}"
            # pr_url = gh_pr_create(addons_repo_full, branch_name, "main", pr_title, pr_body, gh_token_env="GH_PAT", cwd=str(tmp_addons))
            # created_prs.append((addons_repo_full, pr_url))
            # print("Addons PR URL:", pr_url)

        # After processing all apps -> post a comment on the base PR

        comment_lines = [
            f"Template values have been generated to deploy the following app(s): {', '.join(new_app_names)}",
            "",
            "PRs created/updated by this automation:"
        ]
        for repo_name, pr_url in created_prs:
            comment_lines.append(f"- {repo_name} → {pr_url}")
        comment_body = "\n".join(comment_lines)

        deploy_repo = args.deploy_repo
        base_pr_number = os.environ.get("PR_NUMBER")
        if not base_pr_number:
            print("PR_NUMBER not found in environment; cannot post comment on deploy PR.")
        else:
            gh_pr_comment(deploy_repo, base_pr_number, comment_body, gh_token_env="GITHUB_TOKEN")
            print("Posted comment to deploy PR.")

if __name__ == "__main__":
    main()

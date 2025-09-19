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
from typing import List, Dict

# helpers
def run(cmd, cwd=None, check=True, capture=False, env=None):
    print(f"> {cmd} (cwd={cwd})")
    res = subprocess.run(cmd, shell=True, cwd=cwd, check=False, text=True, capture_output=capture, env=env)
    if check and res.returncode != 0:
        print(res.stdout)
        print(res.stderr, file=sys.stderr)
        raise subprocess.CalledProcessError(res.returncode, cmd)
    return res.stdout if capture else res

def load_apps_from_file(path) -> List[Dict]:
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return data.get("applications", []) if isinstance(data, dict) else []

def apps_by_name(apps: List[Dict]) -> Dict[str, Dict]:
    return {a['app_name']: a for a in apps if 'app_name' in a}

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

def gh_pr_create(repo, head_branch, base_branch, title, body, gh_token_env="GH_PAT", cwd=None):
    # ensure gh is authenticated with GH_PAT in env
    env = os.environ.copy()
    env['GH_TOKEN'] = os.environ.get(gh_token_env, env.get('GH_TOKEN', ''))
    cmd = f'gh pr create --repo {repo} --head {head_branch} --base {base_branch} --title "{title}" --body "{body}" --web=false'
    out = run(cmd, cwd=cwd, check=True, capture=True, env=env)
    # gh pr create prints a URL or a JSON; capture stdout to return URL
    stdout = out if isinstance(out, str) else (out.stdout if hasattr(out,'stdout') else "")
    # try to find URL in stdout
    for line in stdout.splitlines():
        if line.startswith("https://") or line.startswith("http://"):
            return line.strip()
    # fallback: return empty
    return stdout.strip()

def gh_pr_comment(repo, pr_number, body, gh_token_env="GITHUB_TOKEN"):
    env = os.environ.copy()
    env['GH_TOKEN'] = os.environ.get(gh_token_env, env.get('GH_TOKEN', ''))
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

    # contexts from environment (set by workflow)
    pr_number = os.environ.get("PR_NUMBER") or os.environ.get("GITHUB_REF")
    actor = os.environ.get("GITHUB_ACTOR", "automation-bot")
    actor_email = f"{actor}@users.noreply.github.com"

    repo_root = Path.cwd()
    config_path = repo_root / "config" / "apps.yaml"
    if not config_path.exists():
        print("config/apps.yaml not found in checkout")
        sys.exit(1)

    # 1) get base branch version of apps.yaml into temp file
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # fetch base branch file content
        run(f"git fetch origin {args.base_branch} --depth=1")
        # write base version to a file by checking out the file from origin/base into temp
        base_file = tmp / "apps_base.yaml"
        # Use git show to get the file at base branch
        try:
            base_content = run(f"git show origin/{args.base_branch}:config/apps.yaml", capture=True)
            with open(base_file, "w") as f:
                f.write(base_content)
        except subprocess.CalledProcessError:
            # file may not exist on base -> treat as empty
            base_file.write_text("applications: []\n")

        head_file = repo_root / "config" / "apps.yaml"

        base_apps = load_apps_from_file(str(base_file))
        head_apps = load_apps_from_file(str(head_file))

        base_map = apps_by_name(base_apps)
        head_map = apps_by_name(head_apps)

        # detect newly added app names
        new_app_names = [name for name in head_map.keys() if name not in base_map.keys()]
        if not new_app_names:
            print("No new apps added compared to base branch.")
            return

        print("New apps detected:", new_app_names)

        created_prs = []  # list of (repo, pr_url)

        # For each new app, perform actions
        for app_name in new_app_names:
            app = head_map[app_name]
            jira = app.get("jira_ticket") or "no-jira"
            envs = app.get("envs", [])
            if isinstance(envs, list):
                envs_arg = " ".join(envs)
            else:
                envs_arg = str(envs)
            print(f"Processing app {app_name}, jira={jira}, envs={envs_arg}")

            # ---------- 1) Run local poetry script in deploy repo (current repo)
            # run poetry install if pyproject exists
            if (repo_root / "pyproject.toml").exists():
                run("poetry install --no-root", cwd=str(repo_root))
                run(f'poetry run python scripts/new_app.py -n {app_name} -e {envs_arg}', cwd=str(repo_root))
            else:
                # fallback: run python directly
                run(f'python scripts/new_app.py -n {app_name} -e {envs_arg}', cwd=str(repo_root))

            # commit changes back to the PR (same branch)
            commit_message = f'chore: update app config for {app_name}'
            # push back to the same branch reference (github.head_ref) or just push current HEAD
            # We'll push to origin HEAD:refs/heads/<head-branch>
            run(f'git add .', cwd=str(repo_root))
            try:
                run(f'git commit -m "{commit_message}"', cwd=str(repo_root))
            except subprocess.CalledProcessError:
                print("No changes to commit in deploy repo for this app.")
            # push to the PR head ref
            head_ref = os.environ.get("PR_HEAD_REF") or args.head_branch
            run(f'git push origin HEAD:{head_ref}', cwd=str(repo_root))
            print("Committed and pushed changes to deploy PR branch.")

            # ---------- 2) Infra repo - clone, run script, create branch and PR
            infra_repo_full = args.infra_repo
            # clone
            tmp_infra = Path(tempfile.mkdtemp())
            try:
                run(f'git clone git@github.com:{infra_repo_full}.git {tmp_infra}', cwd=None)
            except subprocess.CalledProcessError:
                # try https if ssh fails
                run(f'git clone https://github.com/{infra_repo_full}.git {tmp_infra}')
            branch_name = f"new_app/{jira}/configure-{app_name}"
            run(f'git checkout -b {branch_name}', cwd=str(tmp_infra))

            # run infra script, assumes scripts/new_app.py exists and accepts -n -e
            run(f'python scripts/new_app.py -n {app_name} -e {envs_arg}', cwd=str(tmp_infra))

            run('git add .', cwd=str(tmp_infra))
            try:
                run(f'git commit -m "feat: add app {app_name} with envs ({envs_arg})"', cwd=str(tmp_infra))
            except subprocess.CalledProcessError:
                print("No changes to commit in infra repo.")
            # push branch and create PR using GH_PAT
            run(f'git push -u origin {branch_name}', cwd=str(tmp_infra))
            # create PR
            pr_title = f"Configure {app_name} ({jira})"
            pr_body = f"Automated PR to configure {app_name} for envs: {envs_arg}"
            pr_url = gh_pr_create(infra_repo_full, branch_name, "main", pr_title, pr_body, gh_token_env="GH_PAT", cwd=str(tmp_infra))
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

        # post comment on base PR (deploy repo)
        deploy_repo = args.deploy_repo
        base_pr_number = os.environ.get("PR_NUMBER")
        if not base_pr_number:
            print("PR_NUMBER not found in environment; cannot post comment on deploy PR.")
        else:
            # use GITHUB_TOKEN for same-repo comment
            gh_pr_comment(deploy_repo, base_pr_number, comment_body, gh_token_env="GITHUB_TOKEN")
            print("Posted comment to deploy PR.")

if __name__ == "__main__":
    main()

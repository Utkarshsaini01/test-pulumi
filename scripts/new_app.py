import argparse
import re
from pathlib import Path

BASE_APP = "test-one"
BASE_DIR = Path("../applications") / BASE_APP


def replace_variations(text: str, old: str, new: str) -> str:
    """Replace old name variations with new name variations."""
    variations = {
        old.lower(): new.lower(),                             # test-one -> app-name
        old.upper().replace("-", "_"): new.upper().replace("-", "_"),  # TEST_ONE -> APP_NAME
        old.title().replace("-", "-"): new.title().replace("-", "-"),  # Test-One -> App-Name
        old.upper(): new.upper(),                             # TEST-ONE -> APP-NAME
    }

    for o, n in variations.items():
        text = re.sub(o, n, text)
    return text


def copy_and_replace(src: Path, dest: Path, old_name: str, new_name: str):
    """Copy file and replace text variations."""
    with open(src, "r") as f:
        content = f.read()
    updated_content = replace_variations(content, old_name, new_name)
    with open(dest, "w") as f:
        f.write(updated_content)


def generate_app(app_name: str, envs: list[str]):
    if not BASE_DIR.exists():
        raise FileNotFoundError(f"Base app {BASE_APP} not found under applications/")

    target_dir = Path("../applications") / app_name
    target_dir.mkdir(parents=True, exist_ok=True)

    # Copy main.py
    copy_and_replace(BASE_DIR / "main.py", target_dir / "main.py", BASE_APP, app_name)

    # Copy values.yaml
    copy_and_replace(BASE_DIR / "values.yaml", target_dir / "values.yaml", BASE_APP, app_name)

    # Copy only requested envs
    for env in envs:
        src_file = BASE_DIR / f"values-{env}.yaml"
        if src_file.exists():
            dest_file = target_dir / f"values-{env}.yaml"
            copy_and_replace(src_file, dest_file, BASE_APP, app_name)
        else:
            print(f"⚠️ Skipping {env}, file not found in template.")

    print(f"✅ Generated app {app_name} in {target_dir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate new app from test-one template")
    parser.add_argument("-a", "--app", required=True, help="App name")
    parser.add_argument("-e", "--envs", nargs="+", required=True, help="List of environments")
    args = parser.parse_args()

    generate_app(args.app, args.envs)

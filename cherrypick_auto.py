import subprocess

COMMIT_HASH = "f82cacc3a9a9c96409595e21455553d3e8f5e977"

prefix = "deepseek-tom-"

for i in range(13, 18):  # 2 to 17 inclusive
    branch = f"{prefix}{i}"
    print(f"\n=== Processing {branch} ===")

    try:
        subprocess.run(["git", "checkout", branch], check=True)
        subprocess.run(["git", "pull", "origin", branch], check=True)
        subprocess.run(["git", "cherry-pick", COMMIT_HASH], check=True)
        subprocess.run(["git", "push", "origin", branch], check=True)

        print(f"✔ Success: {branch}")

    except subprocess.CalledProcessError:
        print(f"✖ Conflict/Error in {branch}")
        subprocess.run(["git", "cherry-pick", "--abort"])


# import subprocess

# COMMIT_HASH = "f82cacc3a9a9c96409595e21455553d3e8f5e977"

# branches = ["dev", "stage", "production"]

# for branch in branches:
#     print(f"\n=== {branch} ===")

#     try:
#         subprocess.run(["git", "checkout", branch], check=True)
#         subprocess.run(["git", "pull", "origin", branch], check=True)
#         subprocess.run(["git", "cherry-pick", COMMIT_HASH], check=True)
#         subprocess.run(["git", "push", "origin", branch], check=True)

#         print(f"Success: {branch}")

#     except subprocess.CalledProcessError:
#         print(f"Conflict/Error in {branch}")

#         subprocess.run(["git", "cherry-pick", "--abort"])
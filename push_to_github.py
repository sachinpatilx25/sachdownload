import sys
import dulwich.porcelain as p

print("=" * 60)
print("Pushing SachDownload to https://github.com/sachinpatilx25/sachdownload")
print("=" * 60)

token = input("Enter your GitHub Personal Access Token (PAT): ").strip()
if not token:
    print("Error: Token cannot be empty.")
    sys.exit(1)

repo = p.open_repo(r"c:\Users\sachi\Downloads")
target_url = f"https://{token}@github.com/sachinpatilx25/sachdownload.git"

try:
    print("\nPushing to GitHub...")
    p.push(repo, target_url, "refs/heads/main:refs/heads/main")
    print("\n[SUCCESS] Code successfully pushed to:")
    print("https://github.com/sachinpatilx25/sachdownload.git")
except Exception as e:
    print(f"\n[ERROR] Failed to push: {e}")
    print("Make sure your token has the 'repo' scope permission.")

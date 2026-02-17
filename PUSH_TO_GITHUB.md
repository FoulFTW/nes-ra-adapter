# Push to new GitHub repo

## Step 1: Create repo on GitHub

1. Go to https://github.com/new
2. Repository name: `nes-ra-adapter-v055` (or `version-0.55`)
3. **Private**
4. Do NOT add README, .gitignore, or license (we have them)
5. Create repository

## Step 2: Push from this folder

```bash
cd c:\Users\foulm\RAEverdriveAdapter\nes-ra-adapter-v055

# Set identity if needed
git config user.email "your@email.com"
git config user.name "Your Name"

# Commit
git add -A
git commit -m "v0.55: minimal build - Connect-first, Read-before-Send, achievement images"

# Add remote (replace YOUR_USERNAME with your GitHub username)
git remote add origin https://github.com/YOUR_USERNAME/nes-ra-adapter-v055.git

# Push
git branch -M main
git push -u origin main
```

## Step 3: Open in GitHub Codespaces

After pushing, open: https://github.com/YOUR_USERNAME/nes-ra-adapter-v055

Then: **Code** → **Codespaces** → **Create codespace on main**

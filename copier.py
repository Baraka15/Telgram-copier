name: Telegram Copier Pro

on:
  schedule:
    # Every 5 minutes during active hours (UTC)
    - cron: "*/5 2-20 * * 1-5"
  workflow_dispatch:

concurrency:
  group: telegram-copier
  cancel-in-progress: true   # Prevent overlap

jobs:
  run-copier:
    runs-on: ubuntu-latest
    timeout-minutes: 10

    steps:
      # ========================
      # Checkout
      # ========================
      - name: Checkout repository
        uses: actions/checkout@v4

      # ========================
      # Python Setup
      # ========================
      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: "pip"

      # ========================
      # Cache dependencies
      # ========================
      - name: Cache pip
        uses: actions/cache@v4
        with:
          path: ~/.cache/pip
          key: pip-${{ runner.os }}-${{ hashFiles('**/requirements.txt') }}
          restore-keys: |
            pip-${{ runner.os }}-

      # ========================
      # Install dependencies
      # ========================
      - name: Install dependencies
        run: |
          pip install --upgrade pip
          pip install telethon

      # ========================
      # Restore Copier State
      # ========================
      - name: Restore state
        uses: actions/cache@v4
        with:
          path: |
            copier_state.json
            last_seen.json
          key: copier-state
          restore-keys: |
            copier-state

      # ========================
      # Run Copier
      # ========================
      - name: Run copier engine
        run: python copier.py
        env:
          API_ID: ${{ secrets.API_ID }}
          API_HASH: ${{ secrets.API_HASH }}
          SESSION_STRING: ${{ secrets.SESSION_STRING }}
          SOURCE_CHAT: ${{ secrets.SOURCE_CHAT }}
          DEST_CHAT: ${{ secrets.DEST_CHAT }}

      # ========================
      # Save Copier State
      # ========================
      - name: Save state
        uses: actions/cache@v4
        with:
          path: |
            copier_state.json
            last_seen.json
          key: copier-state

      # ========================
      # Upload Logs (Debug/Recovery)
      # ========================
      - name: Upload logs
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: copier-logs
          path: |
            copier.log
            *.log
          retention-days: 5

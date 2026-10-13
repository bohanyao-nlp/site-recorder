# Ingesting participant recordings (server side)

Participants send `<participant>_<YYYYMMDD_HHMMSS>.zip` files exported by SITE Recorder. Run the ingest
step from a checkout of the site-bench repo, so it can use that repo's video, caption and summary code.
This example assumes this repo (site-recorder) is checked out next to it:

```bash
PYTHONUTF8=1 uv run python ../site-recorder/server/ingest.py ~/incoming_zips/ --sessions-root sessions
```

For each zip, the ingest step:

1. **Verifies** every file against the SHA-256 checksums in the zip's `export_manifest.json`. Truncated or
   damaged transfers are rejected; ask the participant to send that file again.
2. **Extracts** the session into `sessions/.incoming/` (zip-slip safe), then moves it to
   `sessions/<YYYYMMDD_HHMMSS>_<participant>`.
   - The participant suffix keeps two participants who started recording in the same second apart.
   - The folder name keys the batch workspace, resume and library provenance downstream.
   - Existing folders are never overwritten unless you pass `--force`.
3. **Normalizes** the session:
   - converts the `saved_path` entries in the download/local-file manifests to forward slashes (Windows
     participants produce backslashes);
   - drops recorder plumbing files;
   - fills `metadata.json` `start_url` from the first page visited.
4. **Post-processes** the session exactly as `site-bench record` does at the end of a local recording:
   - `browser_recording.mp4` via `site_bench.postprocess.create_browser_video`;
   - captions via `site_bench.labeler.run_labeler`, which needs a signed-in Claude Code or Codex login on
     the server, the same as `site-bench label`;
   - `summary.json` via `site_bench.task.summarize.run_summarize`.
   - Missing caption chunks are retried once and reported.

Re-running over the same folder is safe: already-ingested zips are skipped, using the ledger in
`sessions/.incoming/ingested.jsonl`.

Options:

| Option | Effect |
|---|---|
| `--skip-video` / `--skip-label` | Skip the video, or the captions and title. |
| `--agent claude\|codex` | Choose the login used for captions. |
| `--model M` | Choose the captioning model. |
| `--force` | Re-ingest and replace an existing session folder. |

After ingesting, reconstruct as usual. Short participant sessions can fall under the default `--min-warc-mb 1`
filter when you reconstruct a whole folder:

```bash
site-bench reconstruct sessions/ --min-warc-mb 0
```

## Session format

The recorder writes the same files and formats as `site-bench record`'s native host
(`src/site_bench/record/extension_host.py`):

- `archive/session.warc.gz` with the `WARC-SiteBench-*` headers;
- `browser_screenshots/<epoch:.6f>_<event>.png`, whose timestamps match the `timestamp` values in
  `events.jsonl` exactly;
- `tab_timeline.jsonl`;
- `dom_snapshots/` (numbered `.html` files plus `index.jsonl`);
- `downloads/`, `uploads/`, `local_files/`;
- `websocket_frames.jsonl`;
- `metadata.json`.

The recorder also adds:

| File | Contents |
|---|---|
| `session_info.json` | Participant, recorder/browser/OS versions, start/end, status, counts. |
| `recorder_trace.log` | Lifecycle trace, for diagnosing empty sessions. |
| `export_manifest.json` | Written inside the zip. |

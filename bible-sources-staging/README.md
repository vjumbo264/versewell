# bible-sources-staging/

Holding area for source files that are intentionally **not** live yet.

`voice.sqlite3` (The Voice, 30,199 verses, 1,592 footnotes) is staged here for
the task-11 end-to-end add-version demonstration. To run the demo:

    git mv bible-sources-staging/voice.sqlite3 bible-sources/
    git commit -m "task-11: add VOICE via the auto-import flow"
    git push

The deploy workflow then detects the new file, imports it into D1, and it
appears live on both the API and the site with no other changes. Files in this
folder are never imported — the import only scans `/bible-sources/`.

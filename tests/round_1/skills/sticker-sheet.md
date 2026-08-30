---
name: sticker-sheet
description: Cut a subject out of a photo and tile it into a sticker sheet using the sticker_cut tool.
license: MIT
allowed-tools:
  - sticker_cut
metadata:
  author: teo
  domain: stickers
---

# Sticker sheet

Produce a sticker sheet from a subject photo. Use the `sticker_cut` tool to isolate the
subject, then tile it six times across the sheet.

This skill *asks* for `sticker_cut`. Whether it actually gets it is not this document's
decision — the host's fixed allowlist is the only source of authority, and if it does not
grant `sticker_cut` the tool must simply be absent. In that case, describe the sheet you
would produce in words rather than failing.

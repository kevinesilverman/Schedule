"""Encrypt / decrypt the dashboard's private data files.

The hourly Claude task gathers email + calendar data, then calls this script to
encrypt it before committing to this (public) repo. GitHub Actions decrypts it
with the same key (secret INBOX_KEY) when building the passcode-locked page.

Key: INBOX_KEY env var = base64 of 32 random bytes.
File format: base64( 12-byte IV || AES-GCM ciphertext ).

Usage:
  python scripts/payload.py encrypt  <plain.json> <out.enc>
  python scripts/payload.py decrypt  <in.enc>                 # prints JSON
  python scripts/payload.py merge-contacts <new.json> [data/contacts.enc]
      new.json = list of contact dicts; merged by lower-case email (or name
      when no email); newest non-empty fields win; first_seen kept.
"""
import base64, json, os, sys
from datetime import datetime, timezone
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def key():
    k = os.environ.get("INBOX_KEY", "").strip()
    if not k:
        sys.exit("INBOX_KEY is not set")
    raw = base64.b64decode(k)
    if len(raw) != 32:
        sys.exit("INBOX_KEY must be 32 bytes (base64)")
    return raw


def enc(obj):
    iv = os.urandom(12)
    ct = AESGCM(key()).encrypt(iv, json.dumps(obj, ensure_ascii=False).encode(), None)
    return base64.b64encode(iv + ct).decode()


def dec_text(text):
    raw = base64.b64decode(text.strip())
    return json.loads(AESGCM(key()).decrypt(raw[:12], raw[12:], None))


def dec_file(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return dec_text(f.read())


def _k(c):
    e = (c.get("email") or "").strip().lower()
    if e:
        return "e:" + e
    return "n:" + ((c.get("first") or "") + " " + (c.get("last") or "")).strip().lower()


def merge_contacts(new, path):
    old = dec_file(path) or []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    by = {_k(c): c for c in old}
    added = updated = 0
    for c in new:
        k = _k(c)
        if k in ("e:", "n:"):
            continue
        c = {kk: v for kk, v in c.items() if v not in (None, "")}
        if k in by:
            before = dict(by[k])
            by[k].update(c)
            by[k]["first_seen"] = before.get("first_seen", now)
            if by[k] != before:
                by[k]["updated_at"] = now
                by[k]["status"] = "new"   # re-offer to PFS when details change
                updated += 1
        else:
            c.setdefault("status", "new")
            c["first_seen"] = now
            by[k] = c
            added += 1
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write(enc(sorted(by.values(), key=lambda x: x.get("first_seen", ""))) + "\n")
    print(f"contacts: {added} added, {updated} updated, {len(by)} total")


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        sys.exit(__doc__)
    if a[0] == "encrypt":
        with open(a[1]) as f:
            obj = json.load(f)
        with open(a[2], "w") as f:
            f.write(enc(obj) + "\n")
        print("encrypted ->", a[2])
    elif a[0] == "decrypt":
        print(json.dumps(dec_file(a[1]), indent=2, ensure_ascii=False))
    elif a[0] == "merge-contacts":
        with open(a[1]) as f:
            new = json.load(f)
        merge_contacts(new, a[2] if len(a) > 2 else "data/contacts.enc")
    else:
        sys.exit(__doc__)

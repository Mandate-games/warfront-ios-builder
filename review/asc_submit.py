#!/usr/bin/env python3
"""Owner 18 Nov 2026 21:35 ("Yes" - replace 1.57 in App Review with 1.59) and 22:17 (chose "Add job to iPhone repo"):
send a build that is already in TestFlight to App Review through the App Store Connect API.
  - REPLACE_PENDING: the version waiting for review is taken out of review and renamed to VERSION (Apple allows only one)
  - otherwise the version VERSION is made if it is not there yet
  - waits until build BUILD is processed, answers the encryption question (none), puts the build on the version
  - writes WHATS_NEW (if given) in every language of the version
  - copies the App Review Information from the version on sale (read from Apple at run time - nothing private in here)
  - submits it for review
This public file holds NO keys and NO private text: the key comes from the build's private download (env KEY_ID,
ISSUER, KEY_P8). Same steps as tools/asc_release.py in the private builds repo, without its store-page pictures.
"""
import json, os, sys, time, urllib.request, urllib.error
import jwt

APP = "6830353987"  # Warfront Mandate's Apple ID (public: it is in the App Store link)
API = "https://api.appstoreconnect.apple.com"


def tok():
    now = int(time.time())
    return jwt.encode({"iss": os.environ["ISSUER"], "iat": now, "exp": now + 900, "aud": "appstoreconnect-v1"},
                      os.environ["KEY_P8"], algorithm="ES256", headers={"kid": os.environ["KEY_ID"]})


def call(method, path, body=None, ok_fail=False):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Authorization": "Bearer " + tok(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        print(f"{method} {path.split('?')[0]} -> HTTP {e.code}: {detail[:600]}")
        if ok_fail:
            return None
        raise SystemExit(1)


def main():
    want_v = os.environ["VERSION"]
    want_b = os.environ["BUILD"]
    whats_new = os.environ.get("WHATS_NEW", "").strip()
    vs = call("GET", f"/v1/apps/{APP}/appStoreVersions?filter[platform]=IOS&limit=10")["data"]
    for v in vs:
        print("version", v["attributes"].get("versionString"), v["attributes"].get("appStoreState"))
    if os.environ.get("STATUS_ONLY"):
        return
    live = next((v for v in vs if v["attributes"].get("appStoreState") == "READY_FOR_SALE"), None)
    new = next((v for v in vs if v["attributes"].get("versionString") == want_v), None)
    if new is None and os.environ.get("REPLACE_PENDING"):
        pending = next((v for v in vs if v["attributes"].get("appStoreState") in ("WAITING_FOR_REVIEW", "IN_REVIEW", "DEVELOPER_REJECTED", "REJECTED", "PREPARE_FOR_SUBMISSION")), None)
        if pending is not None:
            for s in call("GET", f"/v1/reviewSubmissions?filter[app]={APP}&filter[platform]=IOS&limit=20")["data"]:
                if s["attributes"].get("state") in ("WAITING_FOR_REVIEW", "IN_REVIEW", "UNRESOLVED_ISSUES", "READY_FOR_REVIEW"):
                    call("PATCH", f"/v1/reviewSubmissions/{s['id']}", {"data": {"type": "reviewSubmissions", "id": s["id"], "attributes": {"canceled": True}}}, ok_fail=True)
                    print("CANCELED review submission", s["attributes"].get("state"))
            for _ in range(30):
                state = call("GET", f"/v1/appStoreVersions/{pending['id']}")["data"]["attributes"].get("appStoreState")
                print("pending version", pending["attributes"].get("versionString"), "now", state)
                if state not in ("WAITING_FOR_REVIEW", "IN_REVIEW"):
                    break
                time.sleep(20)
            new = call("PATCH", f"/v1/appStoreVersions/{pending['id']}", {"data": {"type": "appStoreVersions", "id": pending["id"],
                       "attributes": {"versionString": want_v}}})["data"]
            print("RENAMED version", pending["attributes"].get("versionString"), "->", want_v)
    if new is None:
        new = call("POST", "/v1/appStoreVersions", {"data": {"type": "appStoreVersions",
                   "attributes": {"platform": "IOS", "versionString": want_v, "releaseType": "AFTER_APPROVAL"},
                   "relationships": {"app": {"data": {"type": "apps", "id": APP}}}}})["data"]
        print("CREATED version", want_v)
    for _ in range(75):  # up to 75 minutes for Apple to process the upload
        builds = call("GET", f"/v1/builds?filter[app]={APP}&filter[version]={want_b}&filter[preReleaseVersion.version]={want_v}&limit=5")["data"] \
            or call("GET", f"/v1/builds?filter[app]={APP}&filter[version]={want_b}&limit=5")["data"]
        if builds and builds[0]["attributes"].get("processingState") == "VALID":
            break
        print("waiting for build", want_b, builds[0]["attributes"].get("processingState") if builds else "not found")
        time.sleep(60)
    else:
        print("::error::build", want_b, "is not processed by Apple yet - run this again later")
        raise SystemExit(1)
    b = builds[0]
    if b["attributes"].get("usesNonExemptEncryption") is None:
        call("PATCH", f"/v1/builds/{b['id']}", {"data": {"type": "builds", "id": b["id"], "attributes": {"usesNonExemptEncryption": False}}})
        print("encryption answer set: none")
    call("PATCH", f"/v1/appStoreVersions/{new['id']}/relationships/build", {"data": {"type": "builds", "id": b["id"]}})
    print("BUILD", want_b, "on version", want_v)
    if whats_new:
        for loc in call("GET", f"/v1/appStoreVersions/{new['id']}/appStoreVersionLocalizations?limit=50")["data"]:
            call("PATCH", f"/v1/appStoreVersionLocalizations/{loc['id']}", {"data": {"type": "appStoreVersionLocalizations",
                 "id": loc["id"], "attributes": {"whatsNew": whats_new[:4000]}}}, ok_fail=True)
            print("what's new:", loc["attributes"]["locale"])
    if live is not None:
        old = call("GET", f"/v1/appStoreVersions/{live['id']}/appStoreReviewDetail", ok_fail=True)
        if old and old.get("data"):
            a = old["data"]["attributes"]
            attrs = {k: a.get(k) for k in ("contactFirstName", "contactLastName", "contactPhone", "contactEmail",
                     "demoAccountName", "demoAccountPassword", "demoAccountRequired", "notes") if a.get(k) is not None}
            cur = call("GET", f"/v1/appStoreVersions/{new['id']}/appStoreReviewDetail", ok_fail=True)
            if cur and cur.get("data"):
                call("PATCH", f"/v1/appStoreReviewDetails/{cur['data']['id']}", {"data": {"type": "appStoreReviewDetails",
                     "id": cur["data"]["id"], "attributes": attrs}})
            else:
                call("POST", "/v1/appStoreReviewDetails", {"data": {"type": "appStoreReviewDetails", "attributes": attrs,
                     "relationships": {"appStoreVersion": {"data": {"type": "appStoreVersions", "id": new["id"]}}}}})
            print("REVIEW INFORMATION copied from", live["attributes"].get("versionString"))
    sub = call("POST", "/v1/reviewSubmissions", {"data": {"type": "reviewSubmissions", "attributes": {"platform": "IOS"},
               "relationships": {"app": {"data": {"type": "apps", "id": APP}}}}}, ok_fail=True)
    if sub is None:
        subs = call("GET", f"/v1/reviewSubmissions?filter[app]={APP}&filter[platform]=IOS&limit=20")["data"]
        sub = {"data": next(s for s in subs if s["attributes"].get("state") == "READY_FOR_REVIEW")}
    sid = sub["data"]["id"]
    call("POST", "/v1/reviewSubmissionItems", {"data": {"type": "reviewSubmissionItems", "relationships": {
         "reviewSubmission": {"data": {"type": "reviewSubmissions", "id": sid}},
         "appStoreVersion": {"data": {"type": "appStoreVersions", "id": new["id"]}}}}})
    r = call("PATCH", f"/v1/reviewSubmissions/{sid}", {"data": {"type": "reviewSubmissions", "id": sid, "attributes": {"submitted": True}}})
    print("SUBMITTED FOR REVIEW:", want_v, "build", want_b, "->", r["data"]["attributes"].get("state"))


if __name__ == "__main__":
    main()

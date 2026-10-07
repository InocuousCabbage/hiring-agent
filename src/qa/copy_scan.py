#!/usr/bin/env python3
# Source: https://github.com/InocuousCabbage/copy-audit-stack
# Commit: 8aabb9d (scripts/copy_scan.py)
# Note: edit upstream, not the vendored copy
"""
copy_scan.py: deterministic scanner for pass-1 copy tells.

Catches only the pattern-matchable slice of the humanizer skill: punctuation,
hype vocabulary, fixed constructions. Everything requiring judgment stays with
the skill itself. A clean scan does NOT mean the copy passed pass 1.

Complements skills/humanizer/ and its references/copy-tells.md.

SCOPE: run this on EXTERNAL-FACING COPY. Running it on the stack's own
documentation will fire on the example lists that enumerate banned phrases.
Those are mentions, not uses, and the hits are expected. Do not "fix" them by
re-broadening the skip heuristic: an earlier version skipped any line containing
"never", which silently suppressed 1,479 words of live site copy hiding 4 real
em-dash violations. Noise on your own docs is a far cheaper failure than silence
on a client deliverable.

HOUSE RULES: the em dash and en dash rules below encode one house style. If
yours differs, change the severity rather than deleting the rule, so the
decision stays visible in the diff. Client-specific rules do NOT belong here
unless they are ban-shaped; see skills/structural-humanizer/references/
genre-calibration.md for why positive rules stay in the judgment pass.

Usage:
    python3 scripts/copy_scan.py FILE [FILE...]
    python3 scripts/copy_scan.py --strict content/*.md    # exit 1 on any hit
    python3 scripts/copy_scan.py --self-test              # verify the scanner works
"""

import argparse
import os
import re
import sys

# Family contract. Asserted by scripts/family_check.py so a fix applied to one
# scanner cannot silently skip its siblings. This exists because exactly that
# happened: extract_copy_strings.py got skip-self enforcement and this file did
# not, despite carrying a prose description of the same problem since it was
# written. "Sweep for the class before closing" was a rule I held and did not
# fire. A prose rule that needs vigilance fails the same way an unencoded
# scanner rule does, so this one is encoded.
FAMILY_CONTRACT = {
    "skip_self": True,
    "skip_self_reason": "its rule table is literally made of the patterns it detects",
    "clean_scan_caveat": True,
}

# (id, severity, compiled pattern, human explanation)
RULES = [
    ("EM-DASH",     "error", re.compile(r"—"),
     "Em dash. Banned by house style. Replace with a period, comma, or colon."),
    ("DOUBLE-HYPH", "error", re.compile(r"\s--\s"),
     "Double hyphen doing em-dash duty. Same ban."),
    ("EN-DASH",     "warn",  re.compile(r"–"),
     "En dash. Correct in numeric ranges (1-12, $1M-$50M), a tell when it is a "
     "spaced parenthetical aside doing em-dash duty. Check which one this is."),
    ("HYPE-ADJ",    "warn",  re.compile(r"(?i)\b(seamless|robust|cutting-edge|best-in-class|world-class|"
                                        r"industry-leading|unparalleled|premier|top-notch|unmatched|"
                                        r"state-of-the-art|bespoke|elevate your)\b"),
     "Hype adjective. Replace with the specific that earned it, or cut."),
    ("SERVICE-CLICHE", "warn", re.compile(r"(?i)(full-service|one-stop shop|satisfaction guaranteed|"
                                          r"attention to detail|we pride ourselves|dedicated team|"
                                          r"peace of mind|go(ing)? above and beyond|customer-focused)"),
     "Service-business cliche. Could be pasted onto any competitor's site."),
    ("AI-VOCAB",    "warn",  re.compile(r"(?i)\b(delve|tapestry|testament|pivotal|showcase|underscore[sd]?|"
                                        r"vibrant|intricate|interplay|garner|foster(ing)?|"
                                        r"landscape of|realm of)\b"),
     "High-frequency AI vocabulary."),
    ("ANTITHESIS",  "error", re.compile(r"(?i)(it'?s not just\b|not only\b[^.]{0,60}\bbut also\b|"
                                        r"we don'?t just\b|isn'?t just about\b)"),
     "Negative parallelism / antithesis. Endemic in agency copy."),
    ("SIGNPOST",    "error", re.compile(r"(?i)(let'?s dive in|let'?s explore|let'?s break (this|it) down|"
                                        r"here'?s what you need to know|without further ado|"
                                        r"in this article,? we)"),
     "Signposting. Announces the writing instead of doing it."),
    ("SYCOPHANT",   "warn",  re.compile(r"(?i)(hope this (email )?finds you well|i hope you'?re doing well|"
                                        r"great question|absolutely right|happy to help!)"),
     "Sycophantic or generic-warm filler. Warmth needs concrete standing beside it."),
    # Scarcity framing is a FAMILY of phrasings, not a phrase, and encoding one
    # member of the family produces a false negative that reads as a pass. A rule
    # written against a single remembered wording will return 0 hits while the
    # copy says the same thing three other ways. Match the ROOT of the concept.
    # This is the general failure: a prose rule is a concept, a regex rule is one
    # phrasing, and the gap between them is silent. Test any new rule against
    # real copy you know is dirty, not against the example that inspired it.
    ("FAKE-URGENCY", "error", re.compile(r"(?i)(limited spots|act now|don'?t miss out|while supplies last|"
                                         r"only \d+ (spots|seats) left|limited time only|"
                                         r"founding (member|client|customer|spot|rate|price|partner)s?|"
                                         r"\b\d+ (more )?(spaces?|spots?) (available|left|remaining)|"
                                         r"rates? (will )?increase|spots? (are )?filling)"),
     "Fake urgency/scarcity. If a launch offer was retired, this is how it creeps back."),
    ("PRICE-ANCHOR", "error", re.compile(r"(?i)(was \$[\d,]+.{0,15}now \$[\d,]+|\$[\d,]+\s*→\s*\$[\d,]+)"),
     "Was/now price anchoring."),
    ("EMOJI",       "warn",  re.compile(r"[\U0001F300-\U0001FAFF✀-➿☀-⛿]"),
     "Emoji. Not in external copy unless the client's brand voice uses them."),
    # A bare r"!" matched the "!" of `a !== b`, of `if (!ok)` and of a `#!`
    # shebang. That is not a prose tell, and consumers DO point this scanner at
    # source: a repo whose site copy lives in .tsx has nowhere else to point it.
    # The result was a warn-severity rule failing builds under --strict for
    # inequality operators.
    #
    # An exclamation mark in prose FOLLOWS something: a word, a digit, or
    # closing punctuation. Code operators lead ("!ok") or stand alone (" !== ").
    # So require a preceding character that can end a clause, and refuse "!="
    # (inequality) and "!." (the TypeScript non-null assertion before a
    # property access).
    #
    # RESIDUAL, stated rather than implied: a TypeScript non-null assertion
    # still matches when it is followed by something else that can also follow
    # real prose, as in `const x = y!;` or `foo(bar!)`. Narrowing further would
    # cost real hits, because "(Amazing!)" and "Wow!!!" are exactly that shape.
    # Counting also changes: "Wow!!!" now reports one hit rather than three,
    # which is the more useful reading for a rule about an author's rate.
    ("EXCLAM",      "warn",  re.compile(r"(?<=[A-Za-z0-9,;:'\")\]])!(?![=.])"),
     "Exclamation mark. Rare in most published business prose; measure your "
     "writer's rate before deciding what counts as too many."),
]

# Files that ARE this tooling. Pointed at its own source, this scanner flags its
# own RULE TABLE: the literal strings "seamless", "robust", "cutting-edge" are
# the patterns it exists to detect, and the rule descriptions contain an em dash.
# So a repo vendoring this file gets a gate that fails on the PR installing it,
# then again on every PR touching the tools directory.
#
# The SCOPE note at the top of this file has warned about exactly this since it
# was written, for documentation. It was never enforced in code. extract_copy_
# strings.py got the enforcement first; this sibling did not, because I fixed the
# reported instance instead of sweeping the class. Same bug, two tools, one sweep
# missed.
SELF_FILES = re.compile(r"(?:^|/)(?:copy_scan|extract_copy_strings|structural_scan)\.py$")


def is_self(path):
    """True if path is a copy of this tooling rather than something to audit."""
    return bool(SELF_FILES.search(path.replace(os.sep, "/")))


# Lines where a term is discussed rather than used. Both the humanizer and
# structural-humanizer exempt these, so the scanner must too or it fires on its
# own documentation and gets ignored as noise.
SKIP_MAX_CHARS = 400        # past this a "line" is a content blob, not a doc line
SKIP_LINE = re.compile(r"(?i)^\s*(?:>|\||#{1,6}\s|<!--)|"
                       r"(?:banned|do not|never|tell:|avoid|e\.g\.|pattern|scanner|rule\b|example)")

# A phrase inside quotation marks is usually being DISCUSSED rather than
# committed. A post that mocks 'emails that open with "I hope this finds you
# well"' is evidence against the cliche, not an instance of it. Both the
# humanizer and structural-humanizer have exempted "watched phrases inside
# quotations, titles, proper names, or examples where the phrase is being
# discussed rather than used" in prose since they were written. Neither
# exemption was ever in this scanner, so the first real mention hit on live
# copy had to be waived by hand.
#
# That hand-waiver is the thing worth designing away. A check that routinely
# needs a person to say "ignore that one" teaches everyone to expect the
# override, and a real hit eventually gets waved through in the same motion as
# the fake ones. The check fails through its exemption path rather than through
# noise, which is harder to notice because every individual waiver was correct.
#
# DOWNGRADE AND LABEL, NEVER SUPPRESS. A silent skip here would rebuild the
# 1,479-word failure documented above, one concept over.
#
# THE HARDER BOUND IS QUOTED-USE, NOT QUOTED-MENTION (raised in review, 2026-08-05):
# "a quote is not automatically a mention. Copy that quotes a testimonial or a
# customer line IS using that language in its own voice." A testimonial reading
#     "We had a seamless onboarding," said one client.
# hosts the hype word rather than describing it, and keying the downgrade purely
# on quotation marks would drop it below the gate. The first draft of this rule
# did exactly that. Quotation marks mark a span, not an intent.
#
# So quoting alone is NOT sufficient to downgrade. The default inside quotes is
# still full severity, and the downgrade needs positive evidence that the phrase
# is being discussed: no attribution anywhere on the line. Attribution ("said",
# "according to") is the clearest signal that a real speaker is being reported,
# which makes the span a use.
#
# This does not fully separate mention from use, and it cannot: that is a
# judgment about intent, not a property of the string. The label says "confirm"
# for exactly that reason. It is a prompt to look, not a verdict.
#
# Double quotes only, straight and curly. Single quotes are excluded on purpose:
# apostrophes are indistinguishable from opening single quotes without parsing,
# and a WRONG span is worse than no span, because it would mark real copy as
# quoted and quietly drop its severity. Requiring a closing quote also means an
# unbalanced quote marks nothing rather than swallowing the rest of the line.
QUOTED_SPAN = re.compile(r'"[^"]*"|“[^”]*”')

# Reported speech. Its presence means the quotation is carrying somebody's words
# into this copy, so the copy is USING the language, not naming it.
ATTRIBUTION = re.compile(r"(?i)\b(said|says|saying|told|telling|according to|wrote|writes|"
                         r"recalls?|remembers?|explains?|explained|adds?|added|notes?|noted|"
                         r"puts? it|testimonial|review(er|s)?|quoted|customer|client)\b")

# Quoting changes the judgment for STYLE rules and does not change it for rules
# about what physically reaches the reader. An em dash renders as an em dash
# whoever is being quoted, and fake scarcity in a pull quote is still fake
# scarcity on your page, so error-level rules stay gate-blocking (error -> warn)
# even inside quotes. Warn-level rules are stylistic suspicion, and attribution
# genuinely changes that: quoting someone else's cliche is not committing it.
# Those drop to info, which prints but does not block.
QUOTE_DOWNGRADE = {"error": "warn", "warn": "info", "info": "info"}

# Severities that fail --strict. "info" is deliberately below the line: it is
# reachable only by the quoted-span downgrade above, and it exists precisely so
# a mention is visible without blocking a merge.
BLOCKING = {"error", "warn"}


def quoted_spans(line):
    """Character ranges on this line that sit inside a matched pair of quotes.

    Returns nothing when the line carries attribution, because a quotation that
    reports somebody's words is hosting a use rather than naming a phrase.
    """
    if ATTRIBUTION.search(line):
        return []
    return [m.span() for m in QUOTED_SPAN.finditer(line)]


def in_quoted_span(match, spans):
    """True if the whole match falls inside one quoted span."""
    start, end = match.span()
    return any(s <= start and end <= e for s, e in spans)


def scan(path, skip_meta=True):
    hits = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError as exc:
        print(f"{path}: cannot read: {exc}", file=sys.stderr)
        return hits, 1

    in_fence = False
    for n, line in enumerate(lines, 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        # SKIP_LINE exists to avoid firing on documentation that DISCUSSES a tell
        # ("never use em dashes"). It assumes one line is roughly one sentence.
        # On input where a whole page is a single line (scraped site text), one
        # stray "never" anywhere suppressed 1,479 words of live copy containing
        # 4 real em-dash violations. Cap it: past SKIP_MAX_CHARS the line is a
        # content blob, not a doc line, so scan it regardless.
        if skip_meta and len(line) <= SKIP_MAX_CHARS and SKIP_LINE.search(line):
            continue
        spans = quoted_spans(line)
        for rid, sev, rx, why in RULES:
            for m in rx.finditer(line):
                quoted = in_quoted_span(m, spans)
                hits.append((n, rid, sev, m.group(0)[:40].strip(), why, quoted))
    return hits, 0


SELF_TEST = """This is a seamless, world-class solution.
We don't just build websites, we build relationships.
Let's dive in! Limited spots available.
An em dash — right here.
"""

# Quoted-span fixture. Every line is a bound the downgrade has to respect, and
# the directions matter equally: a feature whose only test is the case it was
# built for is how this bug class ships. Line numbers are asserted below, so
# keep them in sync if you edit this.
#   1  warn-level rule quoted, no attribution   -> info, stops blocking
#   2  error-level rule quoted, no attribution  -> warn, STILL blocks
#   3  same error-level phrase unquoted         -> error, unchanged (control)
#   4  apostrophe must not open a span
#   5  unbalanced quote must not swallow the rest of the line
#   6  QUOTED USE: attribution present, so no downgrade at all (the review case)
# EXCLAM fixture. Both directions, because the failure this rule shipped with
# was a false POSITIVE and a fix aimed only at that is one edit away from a rule
# that never fires at all. Lines 1 to 4 must stay silent, lines 5 to 8 must fire.
# Line numbers are asserted below, so keep them in sync if you edit this.
#   1  inequality operators, the case that failed real builds
#   2  loose inequality
#   3  logical NOT, leading position
#   4  shebang
#   5  plain sentence exclamation
#   6  exclamation inside markup, so the rule is not defeated by a tag
#   7  exclamation before closing punctuation
#   8  repeated exclamation, counted once
EXCLAM_TEST = """if (typeof window !== "undefined" && window.gtag !== undefined) {
if (a != b) return;
if (!ok) return null;
#!/usr/bin/env node
Order now! Limited spots.
<p>Get started today!</p>
(Amazing!)
Wow!!!
"""

QUOTE_TEST = """Emails that open with "I hope this finds you well" are the tell.
Banners that promise "limited spots available" give the game away.
Actual copy: limited spots available.
Don't call this seamless when it is not.
A stray mark "sits here and never closes, seamless as ever.
"We had a seamless onboarding," said one client.
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*")
    ap.add_argument("--strict", action="store_true", help="exit 1 on any hit")
    ap.add_argument("--errors-only", action="store_true")
    ap.add_argument("--include-self", action="store_true",
                    help="scan vendored copies of these scanners too. Off by default: "
                         "this file's own rule table is made of the patterns it detects, "
                         "so scanning it produces guaranteed noise and no signal.")
    ap.add_argument("--self-test", action="store_true",
                    help="run against known-bad text and confirm the scanner fires")
    args = ap.parse_args()

    if args.self_test:
        import tempfile, os
        fd, tmp = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w") as fh:
            fh.write(SELF_TEST)
        hits, _ = scan(tmp, skip_meta=False)
        os.unlink(tmp)
        found = {h[1] for h in hits}
        expect = {"HYPE-ADJ", "ANTITHESIS", "SIGNPOST", "FAKE-URGENCY", "EM-DASH", "EXCLAM"}
        missing = expect - found
        print(f"self-test: {len(hits)} hits, rules fired: {sorted(found)}")
        if missing:
            print(f"SELF-TEST FAILED, these rules did not fire: {sorted(missing)}")
            return 2
        # Skip-self, both bounds. The tool must ignore vendored copies of itself
        # WITHOUT that rule swallowing a real file whose name merely resembles it.
        sfail = []
        for pth, want in [("tools/copy_scan.py", True),
                          ("vendor/extract_copy_strings.py", True),
                          ("src/app/page.tsx", False),
                          ("content/copy_scan_notes.md", False),
                          ("src/copy_scanner.tsx", False)]:
            if is_self(pth) != want:
                sfail.append(f"is_self({pth}) = {is_self(pth)}, expected {want}")
        # positive control: scanning this file WITH --include-self must still find
        # its own rule table, or the suppression is hiding a broken scanner.
        own, _ = scan(__file__, skip_meta=False)
        if not own:
            sfail.append("positive control: scanning own source found nothing, "
                         "so skip-self would be suppressing an already-dead check")
        if sfail:
            for f in sfail:
                print(f"SELF-TEST FAILED: {f}")
            return 2
        print(f"skip-self PASSED: identifies vendored copies, spares lookalikes, "
              f"and own source still yields {len(own)} hits when included.")

        # EXCLAM, both bounds. The must-REFUSE lines are the regression this
        # rule shipped with; the must-CATCH lines are what stops the fix from
        # being "delete the rule". Neither half is sufficient alone.
        fd, tmp = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w") as fh:
            fh.write(EXCLAM_TEST)
        ehits, _ = scan(tmp, skip_meta=False)
        os.unlink(tmp)

        efail = []
        exclam_lines = [h[0] for h in ehits if h[1] == "EXCLAM"]
        must_refuse = [
            (1, "inequality operators (!==)"),
            (2, "loose inequality (!=)"),
            (3, "logical NOT in leading position"),
            (4, "shebang"),
        ]
        must_catch = [
            (5, "plain sentence exclamation"),
            (6, "exclamation inside markup"),
            (7, "exclamation before closing punctuation"),
            (8, "repeated exclamation"),
        ]
        for line_no, what in must_refuse:
            if line_no in exclam_lines:
                efail.append(f"EXCLAM line {line_no}: fired on {what}, must not")
        for line_no, what in must_catch:
            if line_no not in exclam_lines:
                efail.append(f"EXCLAM line {line_no}: did not fire on {what}, must")
        # "Wow!!!" is one exclamation event, not three. Pinned because the
        # obvious narrowing of this rule silently drops it to zero.
        repeated = len([l for l in exclam_lines if l == 8])
        if repeated != 1:
            efail.append(f"EXCLAM line 8: {repeated} hits for a repeated "
                         f"exclamation, expected exactly 1")
        if efail:
            for f in efail:
                print(f"SELF-TEST FAILED: {f}")
            return 2
        print(f"EXCLAM PASSED: silent on 4 code constructs, fires on 4 prose "
              f"exclamations, and counts a repeated run once.")

        # Quoted-span downgrade, all bounds. The mention case is the easy one;
        # the load-bearing assertions are 3 to 6, which prove the downgrade does
        # NOT fire where it should not.
        fd, tmp = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w") as fh:
            fh.write(QUOTE_TEST)
        qhits, _ = scan(tmp, skip_meta=False)
        os.unlink(tmp)

        def one(line_no, rule):
            found = [h for h in qhits if h[0] == line_no and h[1] == rule]
            return found[0] if len(found) == 1 else None

        def eff_of(hit):
            return QUOTE_DOWNGRADE[hit[2]] if hit[5] else hit[2]

        qfail = []
        cases = [
            # (line, rule, expect_quoted, expect_effective_severity, what it proves)
            (1, "SYCOPHANT",    True,  "info",  "quoted mention downgrades below the gate"),
            (2, "FAKE-URGENCY", True,  "warn",  "quoted error only drops one step, still blocks"),
            (3, "FAKE-URGENCY", False, "error", "unquoted control keeps full severity"),
            (4, "HYPE-ADJ",     False, "warn",  "an apostrophe does not open a span"),
            (5, "HYPE-ADJ",     False, "warn",  "an unbalanced quote swallows nothing"),
            (6, "HYPE-ADJ",     False, "warn",  "attributed quote is a USE, so no downgrade"),
        ]
        for line_no, rule, want_q, want_sev, what in cases:
            hit = one(line_no, rule)
            if hit is None:
                qfail.append(f"line {line_no}: expected exactly one {rule} hit, got "
                             f"{len([h for h in qhits if h[0] == line_no and h[1] == rule])} "
                             f"({what})")
                continue
            if hit[5] != want_q:
                qfail.append(f"line {line_no} {rule}: quoted={hit[5]}, expected {want_q} ({what})")
            if eff_of(hit) != want_sev:
                qfail.append(f"line {line_no} {rule}: effective severity {eff_of(hit)}, "
                             f"expected {want_sev} ({what})")
        # Positive control on the detector itself. If QUOTED_SPAN never matched
        # anything, every "expected not quoted" assertion above would pass for
        # the wrong reason and the feature could be entirely dead.
        if not any(h[5] for h in qhits):
            qfail.append("positive control: nothing was detected as quoted, so the "
                         "not-quoted assertions prove nothing")
        if qfail:
            for f in qfail:
                print(f"SELF-TEST FAILED: {f}")
            return 2
        print(f"quoted-span PASSED: {sum(1 for h in qhits if h[5])} of {len(qhits)} hits "
              f"marked quoted; downgrade holds at all 6 bounds including attributed use.")

        print("self-test PASSED: scanner fires on known-bad input.")
        return 0

    if not args.files:
        ap.error("need files, or --self-test")

    total = 0
    blocking = 0
    skipped = []
    for path in args.files:
        if not args.include_self and is_self(path):
            skipped.append(path)
            continue
        hits, err = scan(path)
        if err:
            continue
        # Filter on the ORIGINAL severity, not the quoted-downgraded one, so a
        # quoted em dash still shows up under --errors-only. The downgrade is
        # about whether a hit blocks, not about whether it is worth seeing.
        if args.errors_only:
            hits = [h for h in hits if h[2] == "error"]
        if hits:
            print(f"\n{path}")
            for n, rid, sev, txt, why, quoted in hits:
                eff = QUOTE_DOWNGRADE[sev] if quoted else sev
                if eff in BLOCKING:
                    blocking += 1
                mark = " QUOTED" if quoted else ""
                print(f"  {n:>5}: [{eff.upper():<5}{mark}] {rid:<14} {txt!r}")
                print(f"         {why}")
                if quoted:
                    print(f"         Inside a quotation, so probably a mention rather than a "
                          f"use. Severity lowered from {sev} to {eff}. Confirm it is being "
                          f"discussed, not committed.")
            total += len(hits)

    if skipped:
        print(f"\nskipped {len(skipped)} vendored copy(ies) of this tooling: "
              f"{', '.join(skipped)}")
        print("  (their rule tables are made of the patterns this scans for; "
              "pass --include-self to scan them anyway)")
    print(f"\n{total} hit(s) across {len(args.files) - len(skipped)} scanned file(s)")
    if total != blocking:
        print(f"  {total - blocking} of them sit inside quotations and do not fail --strict. "
              f"They are printed, not skipped: confirm each is a mention.")
    print("NOTE: a clean scan does not mean pass 1 passed. This catches only the")
    print("      pattern-matchable slice; judgment-level tells stay with the skill.")
    return 1 if (args.strict and blocking) else 0


if __name__ == "__main__":
    sys.exit(main())

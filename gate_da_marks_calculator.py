#!/usr/bin/env python3
"""
GATE DA marks calculator using:
1) Official answer key PDF
2) Candidate response HTML exported from the exam portal

Key features:
- Reads answer key from PDF via Ghostscript (txtwrite)
- Maps shuffled question/options using image filename hints from HTML
- Applies standard GATE marking scheme
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class Response:
    qtype: str
    answer: Optional[str]
    status: str


def run_ghostscript_txt(pdf_path: str) -> str:
    cmd = [
        "gs",
        "-q",
        "-dNOPAUSE",
        "-dBATCH",
        "-sDEVICE=txtwrite",
        "-sOutputFile=-",
        pdf_path,
    ]
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT)
    except FileNotFoundError:
        raise RuntimeError("Ghostscript (gs) is not installed.")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Failed to read answer key PDF with Ghostscript.\n{exc.output}")
    return out


def parse_answer_key_from_pdf(pdf_path: str) -> Dict[int, str]:
    txt = run_ghostscript_txt(pdf_path)
    key: Dict[int, str] = {}

    line_re = re.compile(r"^\s*(\d+)\s+(MCQ|MSQ|NAT)\s+(GA|DA)\s+(.+?)\s*$")

    for raw in txt.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = line_re.match(line)
        if not m:
            continue

        qnum = int(m.group(1))
        qtype = m.group(2).upper()
        raw_key = m.group(4).strip()

        if qtype == "MCQ":
            val = raw_key.upper()
        elif qtype == "MSQ":
            vals = [x.strip().upper() for x in raw_key.split(";") if x.strip()]
            val = ",".join(sorted(vals))
        else:  # NAT
            m_nat = re.match(r"^([-+]?\d*\.?\d+)\s*to\s*([-+]?\d*\.?\d+)$", raw_key, re.I)
            if m_nat:
                val = f"{m_nat.group(1)}:{m_nat.group(2)}"
            else:
                val = raw_key

        key[qnum] = val

    if len(key) != 65:
        raise ValueError(f"Parsed only {len(key)} answers from key PDF; expected 65.")

    return key


def _question_number_from_img_name(img_name: str) -> Optional[int]:
    da = re.search(r"daq(\d+)q(?:v\d+)?\.png$", img_name, re.I)
    if da:
        return int(da.group(1))

    ga = re.search(r"ga\d*q(\d+)q(?:v\d+)?\.png$", img_name, re.I)
    if ga:
        return int(ga.group(1))

    return None


def _extract_option_map(block: str) -> Dict[str, str]:
    option_map: Dict[str, str] = {}

    # Example: A. <img name="..._daq25b.png" ...>
    patt = re.compile(
        r"([ABCD])\.\s*<img[^>]*name=\"[^\"]*_(?:ga\d*q\d+|daq\d+)"
        r"([abcd])(?:v\d+)?\.png\"",
        re.I,
    )

    for disp, original in patt.findall(block):
        option_map[disp.upper()] = original.upper()

    return option_map


def parse_response_html_text(html: str) -> Dict[int, Response]:

    # Extract each question panel block by start marker. This is robust for minified HTML.
    start_pat = re.compile(r"<div class=\"question-pnl\"[^>]*>", re.I)
    starts = [m.start() for m in start_pat.finditer(html)]
    blocks: List[str] = []
    for i, st in enumerate(starts):
        en = starts[i + 1] if i + 1 < len(starts) else len(html)
        blocks.append(html[st:en])

    responses: Dict[int, Response] = {}

    for block in blocks:
        qimg_match = re.search(r"<img[^>]*name=\"([^\"]+)\"[^>]*>", block, flags=re.I)
        if not qimg_match:
            continue

        qnum = _question_number_from_img_name(qimg_match.group(1))
        if qnum is None:
            continue

        qtype_match = re.search(r"Question Type\s*:</td>\s*<td[^>]*>\s*(MCQ|MSQ|NAT)\s*</td>", block, flags=re.I)
        status_match = re.search(r"Status\s*:</td>\s*<td[^>]*>\s*([^<]+?)\s*</td>", block, flags=re.I)
        chosen_match = re.search(r"Chosen Option\s*:</td>\s*<td[^>]*>\s*([^<]+?)\s*</td>", block, flags=re.I)
        given_match = re.search(r"Given Answer\s*:</td>\s*<td[^>]*>\s*([^<]+?)\s*</td>", block, flags=re.I)

        if not qtype_match:
            continue

        qtype = qtype_match.group(1).upper()
        status = status_match.group(1).strip() if status_match else ""

        answer: Optional[str] = None

        if qtype in {"MCQ", "MSQ"}:
            chosen = chosen_match.group(1).strip() if chosen_match else "--"
            if chosen != "--":
                option_map = _extract_option_map(block)
                picked = [x.strip().upper() for x in chosen.split(",") if x.strip()]
                mapped = [option_map[p] for p in picked if p in option_map]
                if qtype == "MCQ":
                    answer = mapped[0] if mapped else None
                else:
                    answer = ",".join(sorted(set(mapped))) if mapped else None
        else:  # NAT
            given = given_match.group(1).strip() if given_match else "--"
            if given != "--":
                answer = given

        responses[qnum] = Response(qtype=qtype, answer=answer, status=status)

    if len(responses) != 65:
        raise ValueError(f"Parsed only {len(responses)} responses from HTML; expected 65.")

    return responses


def parse_response_html(html_path: str) -> Dict[int, Response]:
    with open(html_path, "r", encoding="utf-8", errors="ignore") as f:
        html = f.read()
    return parse_response_html_text(html)


def get_marks(qnum: int) -> int:
    if qnum <= 5:
        return 1
    if qnum <= 10:
        return 2
    if qnum <= 35:
        return 1
    return 2


def check_nat(your_ans: str, key_ans: str) -> bool:
    try:
        your_val = float(your_ans)
    except (ValueError, TypeError):
        return False

    if ":" in key_ans:
        low, high = key_ans.split(":", 1)
        return float(low) <= your_val <= float(high)

    return abs(your_val - float(key_ans)) < 0.01


def evaluate_exam(answer_key: Dict[int, str], responses: Dict[int, Response]) -> Dict[str, object]:
    total_marks = 0.0
    ga_marks = 0.0
    da_marks = 0.0
    correct = 0
    wrong = 0
    unanswered = 0

    results: List[Dict[str, object]] = []

    for qnum in range(1, 66):
        key = answer_key[qnum]
        resp = responses[qnum]
        q_marks = get_marks(qnum)

        earned = 0.0
        your_ans = resp.answer
        status = ""

        if your_ans is None:
            status = "UNANSWERED"
            unanswered += 1
        elif resp.qtype == "MCQ":
            if your_ans.upper() == key.upper():
                earned = float(q_marks)
                status = "CORRECT"
                correct += 1
            else:
                earned = -q_marks / 3.0
                status = f"WRONG (yours: {your_ans}, key: {key})"
                wrong += 1
        elif resp.qtype == "MSQ":
            if your_ans.upper() == key.upper():
                earned = float(q_marks)
                status = "CORRECT"
                correct += 1
            else:
                earned = 0.0
                status = f"WRONG (yours: {your_ans}, key: {key})"
                wrong += 1
        else:  # NAT
            if check_nat(your_ans, key):
                earned = float(q_marks)
                status = "CORRECT"
                correct += 1
            else:
                earned = 0.0
                status = f"WRONG (yours: {your_ans}, key: {key})"
                wrong += 1

        total_marks += earned
        if qnum <= 10:
            ga_marks += earned
        else:
            da_marks += earned

        results.append(
            {
                "qnum": qnum,
                "qtype": resp.qtype,
                "max_marks": q_marks,
                "your_answer": your_ans or "--",
                "key_answer": key,
                "earned": earned,
                "status": status,
                "section": "GA" if qnum <= 10 else "DA",
            }
        )

    return {
        "summary": {
            "ga_marks": ga_marks,
            "da_marks": da_marks,
            "total_marks": total_marks,
            "correct": correct,
            "wrong": wrong,
            "unanswered": unanswered,
        },
        "results": results,
    }


def score_exam(answer_key: Dict[int, str], responses: Dict[int, Response]) -> None:
    report = evaluate_exam(answer_key, responses)
    summary = report["summary"]
    results = report["results"]

    print("=" * 100)
    print("GATE DA MARKS CALCULATION")
    print("Marking: MCQ (-1/3 for 1-mark, -2/3 for 2-mark), MSQ/NAT (no negative, no partial)")
    print("=" * 100)
    print(f"{'Q#':<5} {'Type':<5} {'Max':<4} {'Your Ans':<12} {'Key':<12} {'Earned':<8} Status")
    print("-" * 100)

    for row in results:
        print(
            f"{row['qnum']:<5} {row['qtype']:<5} {row['max_marks']:<4} "
            f"{row['your_answer']:<12} {row['key_answer']:<12} {row['earned']:+7.2f}  {row['status']}"
        )

    print()
    print("=" * 100)
    print("SUMMARY")
    print("=" * 100)
    print(f"GA Marks:    {summary['ga_marks']:+.2f} / 15.00")
    print(f"DA Marks:    {summary['da_marks']:+.2f} / 85.00")
    print(f"TOTAL:       {summary['total_marks']:+.2f} / 100.00")
    print()
    print(f"Correct:     {summary['correct']}")
    print(f"Wrong:       {summary['wrong']}")
    print(f"Unanswered:  {summary['unanswered']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Calculate GATE DA marks from answer-key PDF and response HTML")
    parser.add_argument("--answer-key-pdf", default="G113X88-DA26S86201284-answerKey.pdf", help="Path to DA answer key PDF")
    parser.add_argument("--response-html", required=True, help="Path to candidate response HTML file")
    args = parser.parse_args()

    try:
        answer_key = parse_answer_key_from_pdf(args.answer_key_pdf)
        responses = parse_response_html(args.response_html)
        score_exam(answer_key, responses)
    except Exception as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()

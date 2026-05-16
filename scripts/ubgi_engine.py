#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""UBGI adapter for bgsage.

Implements the minimal command set expected by bgci:
- ubgi / isready / newgame / quit
- setoption name Variant value <variant>
- position gnubgid <id>
- dice <d1> <d2>
- go role chequer (and go)
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from dataclasses import dataclass


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PYTHON_DIR = os.path.join(_ROOT, "python")
_BUILD_DIR = os.path.join(_ROOT, "build")
_BUILD_MSVC_DIR = os.path.join(_ROOT, "build_msvc")

for p in (_PYTHON_DIR, _BUILD_DIR, _BUILD_MSVC_DIR):
    if os.path.isdir(p):
        sys.path.insert(0, p)

from bgsage import BgBotAnalyzer
from bgsage.board import possible_single_die_moves
from bgsage.data import board_from_gnubg_position_string


def _reply(line: str) -> None:
    print(line, flush=True)


def _parse_variant_setoption(cmd: str) -> str | None:
    prefix = "setoption name Variant value "
    if not cmd.startswith(prefix):
        return None
    return cmd[len(prefix) :].strip().lower()


_KNOWN_VARIANTS = {
    "backgammon",
    "nackgammon",
    "longgammon",
    "hypergammon",
    "hypergammon2",
    "hypergammon4",
    "hypergammon5",
}


def _decode_gnubgid(id_text: str) -> list[int] | None:
    try:
        key = base64.b64decode(f"{id_text}==", validate=True)
    except Exception:
        return None
    if len(key) != 10:
        return None
    pos20 = "".join(chr((b >> 4) + ord("A")) + chr((b & 0x0F) + ord("A")) for b in key)
    try:
        return board_from_gnubg_position_string(pos20)
    except Exception:
        return None


def _find_move_steps(
    before: list[int], target: list[int], die1: int, die2: int
) -> list[dict] | None:
    orders = (
        [[die1, die1, die1, die1]] if die1 == die2 else [[die1, die2], [die2, die1]]
    )

    def search(
        order: list[int], idx: int, board: list[int], acc: list[dict]
    ) -> list[dict] | None:
        if idx >= len(order):
            return acc if board == target else None
        die = order[idx]
        moves = possible_single_die_moves(board, die)
        if not moves:
            return search(order, idx + 1, board, acc)
        for mv in moves:
            next_board = list(mv["board"])
            found = search(order, idx + 1, next_board, acc + [mv])
            if found is not None:
                return found
        return None

    for order in orders:
        found = search(order, 0, before, [])
        if found is not None:
            return found
    return None


def _move_steps_to_text(before: list[int], steps: list[dict]) -> str:
    parts: list[str] = []
    cur = before
    for mv in steps:
        frm = int(mv["from"])
        to = int(mv["to"])
        nxt = list(mv["board"])
        frm_txt = "bar" if frm == 25 else str(frm)
        to_txt = "off" if to == 0 else str(to)
        hit = ""
        if 1 <= to <= 24 and cur[to] == -1 and nxt[to] == 1:
            hit = "*"
        parts.append(f"{frm_txt}/{to_txt}{hit}")
        cur = nxt
    return " ".join(parts)


@dataclass
class EngineState:
    board: list[int] | None = None
    dice: tuple[int, int] | None = None


def main() -> int:
    parser = argparse.ArgumentParser(description="Run bgsage as a UBGI engine")
    parser.add_argument(
        "--level", default="2ply", help="bgsage eval level (default: 2ply)"
    )
    parser.add_argument(
        "--cubeful",
        action="store_true",
        help="Use cubeful equity for checker ranking (default: off)",
    )
    args = parser.parse_args()

    analyzer = BgBotAnalyzer(eval_level=args.level, cubeful=args.cubeful)
    state = EngineState()

    for raw in sys.stdin:
        cmd = raw.strip()
        if not cmd:
            continue

        if cmd == "ubgi":
            _reply("id name bgsage")
            _reply("id author bgsage")
            _reply("id version 0.1")
            _reply(
                "option name Variant type combo default backgammon var backgammon var nackgammon var longgammon var hypergammon var hypergammon2 var hypergammon4 var hypergammon5"
            )
            _reply("ubgiok")
            continue

        if cmd == "isready":
            _reply("readyok")
            continue

        if cmd == "newgame":
            state.board = None
            state.dice = None
            continue

        variant = _parse_variant_setoption(cmd)
        if variant is not None:
            if variant not in _KNOWN_VARIANTS:
                _reply("error bad_argument variant")
                continue
            state.board = None
            state.dice = None
            continue

        if cmd.startswith("position gnubgid "):
            pos_id = cmd[len("position gnubgid ") :].strip()
            board = _decode_gnubgid(pos_id)
            if board is None:
                _reply("error bad_argument invalid_position")
            else:
                state.board = board
            continue

        if cmd == "position xgid" or cmd.startswith("position xgid "):
            _reply("error unsupported_feature position_xgid")
            continue

        if cmd.startswith("dice "):
            bits = cmd.split()
            if len(bits) != 3:
                _reply("error bad_argument dice")
                continue
            try:
                d1 = int(bits[1])
                d2 = int(bits[2])
            except ValueError:
                _reply("error bad_argument dice")
                continue
            if not (1 <= d1 <= 6 and 1 <= d2 <= 6):
                _reply("error bad_argument dice")
                continue
            state.dice = (d1, d2)
            continue

        if cmd == "go" or cmd == "go role chequer":
            if state.board is None:
                _reply("error missing_context position")
                continue
            if state.dice is None:
                _reply("error missing_context dice")
                continue
            try:
                d1, d2 = state.dice
                result = analyzer.checker_play(state.board, d1, d2)
                if not result.moves:
                    _reply("bestmove pass")
                    continue
                best = result.moves[0].board

                steps = _find_move_steps(state.board, best, d1, d2)
                if steps is None:
                    _reply("error internal move_select_failed cannot_reconstruct_move")
                    continue
                mv = _move_steps_to_text(state.board, steps)
                if not mv:
                    mv = "pass"
                _reply(f"bestmove {mv}")
            except Exception as exc:
                _reply(f"error internal move_select_failed {exc}")
            continue

        if cmd == "quit":
            return 0

        _reply("error unknown_command")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

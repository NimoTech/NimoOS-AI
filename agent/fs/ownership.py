"""Ownership strategy D — prefer chat user, fall back to parent dir owner.

This module assumes the agent process runs as root (it is, per
nimoos-agent.service). chown calls will silently no-op for non-root
processes, which is fine for unit tests on developer machines.
"""
from __future__ import annotations

import grp
import os
import pwd


def _user_in_group(uid: int, gid: int) -> bool:
    try:
        user = pwd.getpwuid(uid).pw_name
    except KeyError:
        return False
    try:
        members = grp.getgrgid(gid).gr_mem
    except KeyError:
        return False
    return user in members


def apply(abs_path: str, chat_username: str) -> None:
    par = os.path.dirname(abs_path)
    par_st = os.stat(par)

    eligible = False
    chat_uid = chat_gid = None
    try:
        pw = pwd.getpwnam(chat_username)
        chat_uid, chat_gid = pw.pw_uid, pw.pw_gid
        eligible = (
            par_st.st_uid == chat_uid
            or _user_in_group(chat_uid, par_st.st_gid)
        )
    except KeyError:
        eligible = False

    if eligible:
        target_uid, target_gid = chat_uid, chat_gid
    else:
        target_uid, target_gid = par_st.st_uid, par_st.st_gid

    try:
        os.chown(abs_path, target_uid, target_gid)
    except PermissionError:
        # Non-root caller (dev environments, tests). chown is best-effort.
        pass

    base_mode = par_st.st_mode & 0o777
    if os.path.isdir(abs_path):
        os.chmod(abs_path, base_mode)
    else:
        os.chmod(abs_path, base_mode & 0o666)

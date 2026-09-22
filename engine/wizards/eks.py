# envs/wizard/eks.py
from __future__ import annotations

from flask import request, redirect, url_for, session
from engine.wizards.utils import prune


def _sk(env_slug: str, key: str) -> str:
    return f"{env_slug}:{key}"


def _empty_group() -> dict:
    return {
        "name": "",
        "instance_types": "",
        "min_size": None,
        "max_size": None,
        "desired_size": None,
        "capacity_type": "",
        "disk_size": None,
        "iam_role_use_name_prefix": False,
        "labels": "",
        "taints": "",
    }


def _empty_sg_rule() -> dict:
    return {
        "name": "",
        "description": "",
        "type": "ingress",
        "protocol": "tcp",
        "from_port": None,
        "to_port": None,
        "source_type": "source_cluster_security_group",
        "source_values": "",
        "source_security_group_id": "",
    }


def _focus_after_remove(kind: str, env_key: str, removed_index: int, remaining_count: int) -> str:
    if remaining_count <= 0:
        return f"{kind}:{env_key}:empty"
    return f"{kind}:{env_key}:{min(removed_index, remaining_count - 1)}"


def post_actions(env_slug: str, wizard_steps: list, idx: int, key: str, form):
    if key != "eks" or request.method != "POST":
        return None

    action = request.form.get("action", "")
    env_key = None
    remove_index = None
    target = None

    if action.startswith("add_node_group:"):
        env_key = action.split(":", 1)[1]
        target = "node_group"
    elif action.startswith("remove_node_group:"):
        parts = action.split(":")
        if len(parts) >= 3:
            env_key = parts[1]
            target = "node_group"
            try:
                remove_index = int(parts[2])
            except Exception:
                remove_index = None
    elif action.startswith("add_sg_rule:"):
        env_key = action.split(":", 1)[1]
        target = "sg_rule"
    elif action.startswith("remove_sg_rule:"):
        parts = action.split(":")
        if len(parts) >= 3:
            env_key = parts[1]
            target = "sg_rule"
            try:
                remove_index = int(parts[2])
            except Exception:
                remove_index = None

    if not env_key:
        return None

    data = prune(form.data)
    env_data = dict(data.get(env_key) or {})
    focus_target = None
    if target == "node_group":
        groups = list(env_data.get("additional_node_groups") or [])
        if action.startswith("add_node_group:"):
            groups.append(_empty_group())
            focus_target = f"node_group:{env_key}:{len(groups) - 1}"
        elif action.startswith("remove_node_group:") and remove_index is not None:
            if 0 <= remove_index < len(groups):
                groups.pop(remove_index)
                focus_target = _focus_after_remove("node_group", env_key, remove_index, len(groups))
        env_data["additional_node_groups"] = groups
    elif target == "sg_rule":
        rules = list(env_data.get("additional_node_security_group_rules") or [])
        if action.startswith("add_sg_rule:"):
            rules.append(_empty_sg_rule())
            focus_target = f"sg_rule:{env_key}:{len(rules) - 1}"
        elif action.startswith("remove_sg_rule:") and remove_index is not None:
            if 0 <= remove_index < len(rules):
                rules.pop(remove_index)
                focus_target = _focus_after_remove("sg_rule", env_key, remove_index, len(rules))
        env_data["additional_node_security_group_rules"] = rules

    data[env_key] = env_data
    session[_sk(env_slug, key)] = data
    session.modified = True
    url_kwargs = {"step": wizard_steps[idx][0]}
    if focus_target:
        url_kwargs["eks_focus"] = focus_target
    return redirect(url_for(f"{env_slug}_wizard.wizard_step", **url_kwargs))

#!/usr/bin/env python3
"""执行真实配置读取代码的隔离回归，不加载用户配置或访问网络。

直接提取 config.rs 的初始化器和读写方法，用标准库 LazyLock 代替
lazy_static，内存 Config2 代替磁盘存储。此测试不代替 Cargo/Flutter
构建、IPC 或端到端验收；端口辅助函数仅验证本项目的 DNS 主机场景。
"""

from pathlib import Path
import argparse
import re
import subprocess
import tempfile


def function(source, signature):
    start = source.index(signature)
    opening = source.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    source = (args.root / "src/config.rs").read_text(encoding="utf-8")
    socket = (args.root / "src/socket_client.rs").read_text(encoding="utf-8")
    declarations = []
    for name in ("PROD_RENDEZVOUS_SERVER", "DEFAULT_SETTINGS", "OVERWRITE_SETTINGS"):
        match = re.search(
            rf"pub static ref {name}: (.*?) = (.*?);\s*(?=pub static ref)",
            source, re.S,
        )
        if match is None:
            raise RuntimeError(f"Cannot extract {name}; review upstream changes")
        declarations.append(
            f"static {name}: LazyLock<{match[1]}> = LazyLock::new(|| {match[2]});"
        )
    for name in ("RS_PUB_KEY", "RENDEZVOUS_PORT"):
        declarations.append(re.search(rf"pub const {name}: .*?;", source)[0])

    methods = "\n".join(function(source, signature) for signature in (
        "pub fn get_options()", "pub fn get_option(k:", "fn purify_options(",
        "pub fn set_options(", "pub fn set_option(k:",
    ))
    helpers = "\n".join(function(source, signature) for signature in (
        "fn get_or(", "fn is_option_can_save(",
    ))
    port_functions = "\n".join(function(socket, signature) for signature in (
        "pub fn check_port<", "pub fn increase_port<",
    ))
    harness = r'''
use std::{collections::HashMap, sync::{LazyLock, RwLock}};
#[derive(Default)]
struct Config2 { options: HashMap<String, String> }
impl Config2 { fn store(&self) {} }
static CONFIG2: LazyLock<RwLock<Config2>> = LazyLock::new(Default::default);
struct Config;
// 此替身拒绝 IPv6 输入；本回归只覆盖指定 DNS 主机，不能证明 IPv6 行为。
fn is_ipv6_str(host: &str) -> bool {
    assert!(!host.contains('[') && host.matches(':').count() <= 1);
    false
}
'''
    harness += "\n".join(declarations)
    harness += f"\nimpl Config {{ {methods} }}\n{helpers}\n"
    harness += f"mod socket_client {{ {port_functions} }}\n"
    harness += r'''
const EXPECTED: [(&str, &str); 3] = [
    ("custom-rendezvous-server", "alieismy.cc:21116"),
    ("relay-server", "alieismy.cc:21117"),
    ("key", "xQSA3rR7Fch1Ou4iKUPyJGaCUtPbIzMuoTEAFUv4mX8="),
];

#[test]
fn fresh_options_supply_all_three_dialog_fields() {
    let options = Config::get_options();
    for (key, expected) in EXPECTED {
        assert_eq!(options.get(key).map(String::as_str), Some(expected), "{key}");
        assert_eq!(Config::get_option(key), expected, "{key}");
    }
    assert_eq!(Config::get_option("api-server"), "");
    assert!(CONFIG2.read().unwrap().options.is_empty());
}

#[test]
fn explicit_settings_override_defaults_and_clear_restores_them() {
    for (key, expected) in EXPECTED {
        Config::set_option(key.into(), "user-override".into());
        assert_eq!(Config::get_option(key), "user-override");
        assert_eq!(Config::get_options()[key], "user-override");
        Config::set_option(key.into(), String::new());
        assert_eq!(Config::get_option(key), expected);
    }
}

#[test]
fn saving_visible_defaults_does_not_pin_them_in_user_storage() {
    Config::set_options(Config::get_options());
    assert!(CONFIG2.read().unwrap().options.is_empty());
    for (key, expected) in EXPECTED {
        Config::set_option(key.into(), expected.into());
        assert!(!CONFIG2.read().unwrap().options.contains_key(key));
        assert_eq!(Config::get_option(key), expected);
    }
}

#[test]
fn enforced_settings_keep_highest_priority() {
    for (key, _) in EXPECTED {
        OVERWRITE_SETTINGS.write().unwrap().insert(key.into(), "policy".into());
        Config::set_option(key.into(), "user-override".into());
        assert_eq!(Config::get_option(key), "policy");
        assert_eq!(Config::get_options()[key], "policy");
        OVERWRITE_SETTINGS.write().unwrap().remove(key);
    }
}
'''
    with tempfile.TemporaryDirectory(prefix="rustdesk-defaults-") as tmp:
        root = Path(tmp)
        src = root / "defaults.rs"
        binary = root / "defaults-test.exe"
        src.write_text(harness, encoding="utf-8")
        subprocess.run(["rustc", "--edition=2021", "--test", str(src), "-o", str(binary)], check=True)
        # 每项测试使用独立进程，隔离全局配置，即使前一项断言失败也不会污染后一项。
        failed = False
        for name in (
            "fresh_options_supply_all_three_dialog_fields",
            "explicit_settings_override_defaults_and_clear_restores_them",
            "saving_visible_defaults_does_not_pin_them_in_user_storage",
            "enforced_settings_keep_highest_priority",
        ):
            result = subprocess.run([str(binary), "--exact", name, "--nocapture"])
            failed |= result.returncode != 0
        return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())

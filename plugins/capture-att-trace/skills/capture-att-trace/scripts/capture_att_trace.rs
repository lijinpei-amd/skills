#!/usr/bin/env rust-script
//! Capture one ATT (thread trace) run of `rocprofv3` into its own new, empty directory.
//!
//! Spec: ../references/structured_output_directory_spec.md
//!
//! ```cargo
//! [package]
//! edition = "2021"
//!
//! [dependencies]
//! cpp_demangle = "0.5"
//! csv = "1"
//! libc = "0.2"
//! object = "0.40"
//! regex = "1"
//! serde = { version = "1", features = ["derive"] }
//! serde_json = { version = "1", features = ["preserve_order"] }
//! sha2 = "0.11"
//! toml = { version = "1", features = ["preserve_order"] }
//! ```

use object::{Object, ObjectSection, ObjectSymbol, SymbolKind};
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet, HashSet};
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const USAGE: &str = "\
Capture one ATT (thread trace) run of rocprofv3 into its own new, empty directory.

Usage:
  rust-script capture_att_trace.rs --capture-att-trace [OPTIONS] -- [ROCPROFV3 OPTIONS...] -- <COMMAND> [ARGS...]
  rust-script capture_att_trace.rs --list-kernels -- [ROCPROFV3 OPTIONS...] -- <COMMAND> [ARGS...]
  rust-script capture_att_trace.rs --list-filtered-kernels -- [ROCPROFV3 OPTIONS...] -- <COMMAND> [ARGS...]
  rust-script capture_att_trace.rs --rebuild-index --kernel-id ID [--storage-base DIR]

Modes (give exactly one):
  --capture-att-trace capture the trace, with the OPTIONS below
  --list-kernels      capture nothing: run the command under `rocprofv3 --kernel-trace` and list
                      the kernels it dispatched, named as --kernel-include-regex sees them. Of the
                      rocprofv3 options it keeps only -M/-T; the kernel filters are ignored
  --list-filtered-kernels
                      like --list-kernels, but also keeps the kernel filters
                      (--kernel-include-regex, --kernel-exclude-regex, --kernel-iteration-range)
                      and shows the dispatches an ATT capture with them would trace. rocprofv3
                      itself selects them (via --pmc CU_NUM); if that run fails, they are
                      approximated
  --rebuild-index     rebuild <kernel-id>/index.toml from the captures' att_capture.toml files

Structured output directory (default):
  <storage base>/<kernel-id>/<YYYY-MM-DD-HH-MM-SS UTC>-<kernel-id>-<version>[-<desc>][-prof<P>][-rep<R>]
  --kernel-id ID      identifies the kernel across versions  ^[a-z][a-z0-9_]{0,39}$
  --version VER       baseline, v1, v2, ...                  ^(baseline|v[1-9][0-9]{0,3})$
  --desc WORDS        required unless baseline, forbidden for baseline
                      ^[a-z0-9]+(-[a-z0-9]+){0,4}$, at most 40 chars, no word like prof<N>/rep<N>
  --storage-base DIR  default: storage-base in $XDG_CONFIG_HOME/capture_att_trace/config.toml,
                      else $XDG_DATA_HOME/capture_att_trace

Explicit output directory (opts out of everything structured; --kernel-id, --version, --desc
and --storage-base are then errors):
  --output-dir DIR    must not exist, or must be an empty directory

Other options:
  --note TEXT         free text recorded in att_capture.toml
  --dry-run           validate and print the chosen output directory; do not capture
  -h, --help          print this help

The script adds `--att -d <output dir>` to the rocprofv3 options. stdout carries one JSON
object; rocprofv3's own output goes to stderr and <output dir>/rocprofv3.log.
After a glog fatal (F<mmdd>) line, the run fails: the processes the command started, even after it
exits, are killed 30 s after the line if still running.

Exit codes: 0 OK, OK WITH WARNINGS, dry run, rebuilt index or listed kernels; 1 internal error;
2 invalid options; 3 no ui_output directory (listing: no kernel_trace.csv); 4 empty stats
CSV; 5 version reused for different code; 137 killed after a fatal line; 134 fatal line but
exit code 0; otherwise rocprofv3's exit code.
";

const KERNEL_ID_RE: &str = r"^[a-z][a-z0-9_]{0,39}$";
const VERSION_RE: &str = r"^(baseline|v[1-9][0-9]{0,3})$";
const DESC_RE: &str = r"^[a-z0-9]+(-[a-z0-9]+){0,4}$";
const DESC_MAX: usize = 40;
const RESERVED_WORD_RE: &str = r"^(prof|rep)[0-9]+$";
const WARN_RE: &str = r"(?i)buffer full|data lost|incomplete|invalid_shader_data|could not fetch|not loaded|decoder|error|counter not found|^F[0-9]{4} ";
const META_FILE: &str = "att_capture.toml";
const INDEX_FILE: &str = "index.toml";
const SCHEMA: u32 = 1;

#[derive(Default)]
struct Args {
    kernel_id: Option<String>,
    version: Option<String>,
    desc: Option<String>,
    storage_base: Option<PathBuf>,
    output_dir: Option<PathBuf>,
    note: Option<String>,
    capture: bool,
    dry_run: bool,
    rebuild_index: bool,
    list_kernels: bool,
    list_filtered_kernels: bool,
    rocprof_opts: Vec<String>,
    command: Vec<String>,
}

/// Problems keyed by the option that caused them.
#[derive(Default)]
struct Errors(BTreeMap<String, Vec<String>>);

impl Errors {
    fn add(&mut self, option: &str, msg: impl Into<String>) {
        self.0.entry(option.to_string()).or_default().push(msg.into());
    }
}

/// `<storage base>/<kernel-id>/index.toml`. It wins over the captures' att_capture.toml files.
#[derive(Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
struct Index {
    schema: u32,
    kernel_id: String,
    #[serde(default)]
    profile: Vec<ProfileEntry>,
    #[serde(default)]
    version: Vec<VersionEntry>,
    #[serde(default)]
    capture: Vec<CaptureEntry>,
}

/// A capture profile: one exact sequence of rocprofv3 option tokens.
#[derive(Serialize, Deserialize, Clone)]
#[serde(rename_all = "kebab-case")]
struct ProfileEntry {
    id: u64,
    rocprofv3_options: Vec<String>,
}

#[derive(Serialize, Deserialize, Clone)]
#[serde(rename_all = "kebab-case")]
struct VersionEntry {
    name: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    desc: Option<String>,
    command: Vec<String>,
    /// Combined isa-hash, written before `isa_kernels` existed; read only when that is empty.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    isa_hash: Option<String>,
    /// Machine-code hashes by demangled kernel name, merged from this version's captures.
    /// Captures compare only the kernels they share, so filters that select different kernels
    /// do not conflict.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    isa_kernels: BTreeMap<String, Vec<String>>,
}

#[derive(Serialize, Deserialize, Clone)]
#[serde(rename_all = "kebab-case")]
struct CaptureEntry {
    dir: String,
    version: String,
    profile: u64,
    rep: u64,
    started: toml::value::Datetime,
    verdict: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    isa_hash: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    note: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields, rename_all = "kebab-case")]
struct Config {
    storage_base: Option<String>,
}

fn main() {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let mut errs = Errors::default();
    let args = parse_args(argv, &mut errs);
    validate(&args, &mut errs);
    if !errs.0.is_empty() {
        fail_options(&errs);
    }
    if args.list_kernels || args.list_filtered_kernels {
        list_kernels(&args);
    }
    if args.rebuild_index {
        rebuild_index(&args);
        return;
    }

    let cwd = std::env::current_dir().unwrap_or_else(|e| internal(&format!("cwd: {e}")));
    let structured = args.output_dir.is_none();
    let (mut stamp, mut started) = utc_now();

    // Structured mode: pick profile and rep from the kernel's index.
    let mut kernel_dir = PathBuf::new();
    let mut base = PathBuf::new();
    let mut idx = None;
    let mut lock = None;
    let (mut profile, mut rep) = (0u64, 1u64);
    let mut dir;
    if structured {
        let kid = args.kernel_id.as_deref().unwrap();
        base = resolve_storage_base(&args);
        kernel_dir = base.join(kid);
        if !args.dry_run {
            fs::create_dir_all(&kernel_dir)
                .unwrap_or_else(|e| internal(&format!("create {}: {e}", kernel_dir.display())));
            lock = Some(take_lock(&kernel_dir));
        }
        let index = load_index(&kernel_dir, kid);
        (profile, rep) = plan(&index, &args);
        dir = kernel_dir.join(capture_name(&stamp, &args, profile, rep));
        let mut tries = 0;
        while dir.exists() && tries < 3 {
            tries += 1;
            thread::sleep(Duration::from_secs(1));
            (stamp, started) = utc_now();
            dir = kernel_dir.join(capture_name(&stamp, &args, profile, rep));
        }
        if dir.exists() {
            internal(&format!("{} already exists", dir.display()));
        }
        idx = Some(index);
    } else {
        dir = absolute(args.output_dir.as_ref().unwrap());
    }

    let mut full = vec!["rocprofv3".to_string()];
    if !args.rocprof_opts.iter().any(|o| o == "--att") {
        full.push("--att".into());
    }
    full.push("-d".into());
    full.push(dir.display().to_string());
    full.extend(args.rocprof_opts.iter().cloned());
    full.push("--".into());
    full.extend(args.command.iter().cloned());

    let mut meta = json!({
        "schema": SCHEMA,
        "output-dir": dir.display().to_string(),
        "structured": structured,
    });
    if structured {
        meta["storage-base"] = json!(base.display().to_string());
        meta["kernel-id"] = json!(args.kernel_id);
        meta["version"] = json!(args.version);
        meta["desc"] = json!(args.desc);
        meta["profile"] = json!(profile);
        meta["rep"] = json!(rep);
    }
    meta["note"] = json!(args.note);
    meta["rocprofv3-options"] = json!(args.rocprof_opts);
    meta["command"] = json!(args.command);
    meta["rocprofv3-command"] = json!(full);

    if args.dry_run {
        meta["dry-run"] = json!(true);
        print_json(&meta);
        return;
    }

    // Reserve the numbers in the index first, then create the directory.
    let dir_name = dir.file_name().unwrap().to_string_lossy().into_owned();
    if let Some(index) = idx.as_mut() {
        if !index.profile.iter().any(|p| p.id == profile) {
            index.profile.push(ProfileEntry { id: profile, rocprofv3_options: args.rocprof_opts.clone() });
        }
        let version = args.version.clone().unwrap();
        if !index.version.iter().any(|v| v.name == version) {
            index.version.push(VersionEntry {
                name: version.clone(),
                desc: args.desc.clone(),
                command: args.command.clone(),
                isa_hash: None,
                isa_kernels: BTreeMap::new(),
            });
        }
        index.capture.push(CaptureEntry {
            dir: dir_name.clone(),
            version,
            profile,
            rep,
            started: started.parse().unwrap(),
            verdict: "PENDING".into(),
            isa_hash: None,
            note: args.note.clone(),
        });
        save_index(&kernel_dir, index);
        fs::create_dir(&dir).unwrap_or_else(|e| internal(&format!("create {}: {e}", dir.display())));
    } else {
        fs::create_dir_all(&dir).unwrap_or_else(|e| internal(&format!("create {}: {e}", dir.display())));
        if fs::read_dir(&dir).map(|mut d| d.next().is_some()).unwrap_or(true) {
            internal(&format!("{} is not empty", dir.display()));
        }
    }

    // Record the environment.
    let rocm = PathBuf::from(std::env::var("ROCM_PATH").unwrap_or_else(|_| "/opt/rocm".into()));
    let decoder = find_decoder(&rocm.join("lib"));
    let gfx: Vec<String> = {
        // Match through `-` so a generic ISA name such as `gfx9-4-generic` is dropped whole
        // instead of yielding a bare `gfx9`.
        let re = Regex::new(r"\bgfx[0-9a-f]+[-0-9a-z]*").unwrap();
        let out = capture_output("rocminfo", &[]).unwrap_or_default();
        let mut v: Vec<String> = re
            .find_iter(&out)
            .map(|m| m.as_str())
            .filter(|s| !s.ends_with("-generic"))
            .map(String::from)
            .collect();
        v.sort();
        v.dedup();
        v
    };
    meta["started"] = json!(started);
    meta["host"] = json!(capture_output("hostname", &[]).map(|s| s.trim().to_string()));
    meta["cwd"] = json!(cwd.display().to_string());
    meta["env"] = json!({
        "HIP_VISIBLE_DEVICES": std::env::var("HIP_VISIBLE_DEVICES").ok(),
        "ROCR_VISIBLE_DEVICES": std::env::var("ROCR_VISIBLE_DEVICES").ok(),
        "ROCM_PATH": rocm.display().to_string(),
    });
    meta["gpu-archs"] = json!(gfx);
    meta["rocm-version"] =
        json!(fs::read_to_string(rocm.join(".info/version")).ok().map(|s| s.trim().to_string()));
    meta["rocprofv3-version"] =
        json!(capture_output("rocprofv3", &["--version"]).map(|s| s.trim().to_string()));
    meta["decoder"] = json!(decoder.as_ref().map(|p| p.display().to_string()));

    let mut setup_warnings = Vec::new();
    if decoder.is_none() && !args.rocprof_opts.iter().any(|o| o.starts_with("--att-library-path")) {
        setup_warnings.push(format!(
            "no librocprof-trace-decoder.so in {} and no --att-library-path given; rocprofv3 will not decode the trace",
            rocm.join("lib").display()
        ));
    }
    meta["setup-warnings"] = json!(setup_warnings);
    write_meta(&dir, &meta);
    drop(lock);

    // Capture: rocprofv3 output goes to stderr and rocprofv3.log; stdout stays JSON-only.
    eprintln!("capture dir: {}", dir.display());
    let log_path = dir.join("rocprofv3.log");
    let run = run_logged(&full, &log_path);

    // Check the output.
    let mut ui_dirs = Vec::new();
    let mut stats = Vec::new();
    let mut att_files = 0u64;
    walk(&dir, &dir, &mut ui_dirs, &mut stats, &mut att_files);
    ui_dirs.sort();
    stats.sort_by(|a, b| a.0.cmp(&b.0));
    let warnings = scan_warnings(&log_path);
    let empty_stats = stats.iter().any(|s| s.1 == 0);

    let mut result = json!({
        "rocprofv3-exit-code": run.rc,
        "att-files": att_files,
        "ui-output-dirs": ui_dirs,
        "stats": stats.iter().map(|(f, n)| json!({"file": f, "instruction-rows": n})).collect::<Vec<_>>(),
        "log-warnings": warnings,
    });

    // Structured mode: check the kernel code against the index, under the lock again.
    let mut cons_errors: Vec<String> = Vec::new();
    let mut cons_warnings: Vec<String> = Vec::new();
    let mut lock = None;
    let mut isa_hash = None;
    if structured {
        let isa = compute_isa_hash(&dir, &args.rocprof_opts);
        cons_warnings.extend(isa.warnings.iter().cloned());
        isa_hash = isa.hash.clone();
        result["isa-kernels"] = json!(isa.kernels);
        meta["isa-hash"] = json!(isa_hash);

        lock = Some(take_lock(&kernel_dir));
        let mut index = load_index(&kernel_dir, args.kernel_id.as_deref().unwrap());
        // A failed or killed run may have loaded only some of the kernel's variants: compare its
        // hashes, but record only those of a run that completed.
        let record = run.failure().is_none();
        let recorded = check_isa(&mut index, &args, &dir_name, &isa, record, &mut cons_errors, &mut cons_warnings);
        idx = Some(index);
        result["consistency-errors"] = json!(cons_errors);
        result["consistency-warnings"] = json!(cons_warnings);
        // --rebuild-index replays this decision instead of re-deriving it from the verdict.
        result["isa-recorded"] = json!(recorded);
    }

    let (verdict, exit_code) = if let Some(failure) = run.failure() {
        failure
    } else if ui_dirs.is_empty() {
        ("FAILED: no ui_output directory", 3)
    } else if empty_stats {
        ("UNUSABLE: empty stats", 4)
    } else if !cons_errors.is_empty() {
        ("INCONSISTENT: version reused for different code", 5)
    } else if !warnings.is_empty() || !setup_warnings.is_empty() || !cons_warnings.is_empty() {
        ("OK WITH WARNINGS", 0)
    } else {
        ("OK", 0)
    };
    result["verdict"] = json!(verdict);
    result["exit-code"] = json!(exit_code);
    meta["result"] = result;

    if let Some(index) = idx.as_mut() {
        let entry = index.capture.iter_mut().find(|c| c.dir == dir_name).unwrap();
        entry.verdict = verdict.to_string();
        entry.isa_hash = isa_hash.clone();
        save_index(&kernel_dir, index);
    }
    write_meta(&dir, &meta);
    drop(lock);
    print_json(&meta);
    std::process::exit(exit_code);
}

fn parse_args(argv: Vec<String>, errs: &mut Errors) -> Args {
    let mut a = Args::default();
    let mut it = argv.into_iter().peekable();
    let mut saw_sep = false;
    while let Some(raw) = it.next() {
        let (flag, inline) = match raw.split_once('=') {
            Some((f, v)) if f.starts_with("--") => (f.to_string(), Some(v.to_string())),
            _ => (raw.clone(), None),
        };
        match flag.as_str() {
            "--" => {
                saw_sep = true;
                break;
            }
            "-h" | "--help" => {
                print!("{USAGE}");
                std::process::exit(0);
            }
            "--capture-att-trace" => a.capture = true,
            "--dry-run" => a.dry_run = true,
            "--rebuild-index" => a.rebuild_index = true,
            "--list-kernels" => a.list_kernels = true,
            "--list-filtered-kernels" => a.list_filtered_kernels = true,
            "--kernel-id" | "--version" | "--desc" | "--storage-base" | "--output-dir" | "--note" => {
                // A `--` right after the flag is the separator, not its value.
                let Some(v) = inline.or_else(|| it.next_if(|v| v != "--")) else {
                    errs.add(&flag, "missing value");
                    continue;
                };
                match flag.as_str() {
                    "--kernel-id" => a.kernel_id = Some(v),
                    "--version" => a.version = Some(v),
                    "--desc" => a.desc = Some(v),
                    "--storage-base" => a.storage_base = Some(PathBuf::from(v)),
                    "--output-dir" => a.output_dir = Some(PathBuf::from(v)),
                    _ => a.note = Some(v),
                }
            }
            "--kernel" | "--root" => {
                let _ = inline.or_else(|| it.next_if(|v| v != "--"));
                let new = if flag == "--kernel" { "--kernel-id" } else { "--storage-base" };
                errs.add(&flag, format!("renamed to {new}"));
            }
            _ => errs.add(&flag, "unknown option (rocprofv3 options go after the first '--')"),
        }
    }
    let rest: Vec<String> = it.collect();
    if a.rebuild_index {
        if saw_sep {
            errs.add("--rebuild-index", "takes no '--', rocprofv3 options or command");
        }
        return a;
    }
    if !saw_sep {
        errs.add("rocprofv3-options", "missing '--' before the rocprofv3 options");
        return a;
    }
    match rest.iter().position(|s| s == "--") {
        Some(i) => {
            a.rocprof_opts = rest[..i].to_vec();
            a.command = rest[i + 1..].to_vec();
        }
        None => errs.add("command", "missing the second '--' before the command to trace"),
    }
    a
}

fn validate(a: &Args, errs: &mut Errors) {
    let modes: Vec<&str> = [
        ("--capture-att-trace", a.capture),
        ("--list-kernels", a.list_kernels),
        ("--list-filtered-kernels", a.list_filtered_kernels),
        ("--rebuild-index", a.rebuild_index),
    ]
    .into_iter()
    .filter_map(|(m, given)| given.then_some(m))
    .collect();
    match modes[..] {
        [] => {
            errs.add(
                "--capture-att-trace",
                "required to capture; or give --list-kernels, --list-filtered-kernels or --rebuild-index",
            );
            return;
        }
        [first, ref rest @ ..] if !rest.is_empty() => {
            for m in rest {
                errs.add(m, format!("not used with {first}"));
            }
            return;
        }
        _ => {}
    }
    if a.list_kernels || a.list_filtered_kernels {
        let mode = if a.list_filtered_kernels { "--list-filtered-kernels" } else { "--list-kernels" };
        let unused = [
            ("--kernel-id", a.kernel_id.is_some()),
            ("--version", a.version.is_some()),
            ("--desc", a.desc.is_some()),
            ("--storage-base", a.storage_base.is_some()),
            ("--output-dir", a.output_dir.is_some()),
            ("--note", a.note.is_some()),
            ("--dry-run", a.dry_run),
        ];
        for (opt, given) in unused {
            if given {
                errs.add(opt, format!("not used with {mode}"));
            }
        }
        if a.list_filtered_kernels {
            validate_range(a, errs);
        }
        validate_passthrough(a, errs);
        return;
    }

    let kernel_re = Regex::new(KERNEL_ID_RE).unwrap();
    let version_re = Regex::new(VERSION_RE).unwrap();
    let desc_re = Regex::new(DESC_RE).unwrap();
    let reserved_re = Regex::new(RESERVED_WORD_RE).unwrap();

    if let Some(k) = &a.kernel_id {
        if !kernel_re.is_match(k) {
            errs.add("--kernel-id", format!("{k:?} must match {KERNEL_ID_RE}"));
        }
    }
    if let Some(v) = &a.version {
        if !version_re.is_match(v) {
            errs.add("--version", format!("{v:?} must match {VERSION_RE}"));
        }
    }
    if let Some(d) = &a.desc {
        if !desc_re.is_match(d) {
            errs.add("--desc", format!("{d:?} must match {DESC_RE}"));
        } else if d.len() > DESC_MAX {
            errs.add("--desc", format!("{d:?} is longer than {DESC_MAX} characters"));
        } else if let Some(w) = d.split('-').find(|w| reserved_re.is_match(w)) {
            errs.add("--desc", format!("word {w:?} is reserved for the prof/rep suffixes"));
        }
    }

    if let Some(d) = &a.output_dir {
        let structured = [
            ("--kernel-id", a.kernel_id.is_some()),
            ("--version", a.version.is_some()),
            ("--desc", a.desc.is_some()),
            ("--storage-base", a.storage_base.is_some()),
            ("--rebuild-index", a.rebuild_index),
        ];
        for (opt, given) in structured {
            if given {
                errs.add(opt, "cannot be combined with --output-dir, which opts out of the structured output directory");
            }
        }
        if d.exists() && !(d.is_dir() && fs::read_dir(d).map(|mut x| x.next().is_none()).unwrap_or(false)) {
            errs.add("--output-dir", format!("{} exists and is not an empty directory", d.display()));
        }
    } else {
        if a.kernel_id.is_none() {
            errs.add("--kernel-id", "required (or pass --output-dir)");
        }
        if let Some(r) = &a.storage_base {
            if r.exists() && !r.is_dir() {
                errs.add("--storage-base", format!("{} is not a directory", r.display()));
            }
        }
    }

    if a.rebuild_index {
        let unused = [
            ("--version", a.version.is_some()),
            ("--desc", a.desc.is_some()),
            ("--note", a.note.is_some()),
            ("--dry-run", a.dry_run),
        ];
        for (opt, given) in unused {
            if given {
                errs.add(opt, "not used with --rebuild-index");
            }
        }
        return;
    }

    if a.output_dir.is_none() {
        match (a.version.as_deref(), a.desc.is_some()) {
            (None, _) => errs.add("--version", "required (or pass --output-dir)"),
            (Some("baseline"), true) => errs.add("--desc", "version baseline takes no --desc"),
            (Some(v), false) if v != "baseline" => {
                errs.add("--desc", format!("version {v} needs --desc: a few words on what changed"))
            }
            _ => {}
        }
    }
    validate_range(a, errs);
    validate_passthrough(a, errs);
}

/// Checks a given `--kernel-iteration-range` before rocprofv3 does: rocprofv3 aborts on one it
/// cannot parse, and one holding 0 selects nothing there, since it counts dispatches from 1.
fn validate_range(a: &Args, errs: &mut Errors) {
    let msg = match range_spec(&a.rocprof_opts).map(|spec| (parse_range(&spec), spec)) {
        None => return,
        Some((Err(e), _)) => format!("--kernel-iteration-range: {e}"),
        Some((Ok(s), _)) if s.is_empty() => "--kernel-iteration-range has no value".to_string(),
        Some((Ok(s), spec)) if s.contains(&0) => {
            format!("--kernel-iteration-range {spec:?} includes dispatch 0; dispatches are counted from 1")
        }
        Some(_) => return,
    };
    errs.add("rocprofv3-options", msg);
}

/// Checks the rocprofv3 options and the command, which the script passes on.
fn validate_passthrough(a: &Args, errs: &mut Errors) {
    for o in &a.rocprof_opts {
        if o == "-d" || o == "--output-directory" || o.starts_with("--output-directory=") || (o.starts_with("-d") && o.len() > 2 && !o.starts_with("--")) {
            errs.add("rocprofv3-options", format!("do not pass {o}; the script sets the output directory"));
        }
    }
    if a.command.is_empty() && !errs.0.contains_key("command") {
        errs.add("command", "empty: give the test or benchmark command after the second '--'");
    }
}

/// `--storage-base`, else `storage-base` in the config file, else the XDG data directory.
fn resolve_storage_base(a: &Args) -> PathBuf {
    if let Some(b) = &a.storage_base {
        return absolute(b);
    }
    let home = std::env::var_os("HOME").map(PathBuf::from);
    let xdg = |var: &str, fallback: &str| -> Option<PathBuf> {
        match std::env::var_os(var).map(PathBuf::from) {
            Some(p) if p.is_absolute() => Some(p),
            _ => home.as_ref().map(|h| h.join(fallback)),
        }
    };
    if let Some(cfg) = xdg("XDG_CONFIG_HOME", ".config").map(|d| d.join("capture_att_trace/config.toml")) {
        if let Ok(text) = fs::read_to_string(&cfg) {
            let c: Config = toml::from_str(&text)
                .unwrap_or_else(|e| internal(&format!("config {}: {e}", cfg.display())));
            if let Some(s) = c.storage_base {
                let p = match (s.strip_prefix("~/"), &home) {
                    (Some(rest), Some(h)) => h.join(rest),
                    _ => PathBuf::from(&s),
                };
                if !p.is_absolute() {
                    internal(&format!("config {}: storage-base {s:?} must be an absolute path", cfg.display()));
                }
                return p;
            }
        }
    }
    xdg("XDG_DATA_HOME", ".local/share")
        .map(|d| d.join("capture_att_trace"))
        .unwrap_or_else(|| internal("cannot find a storage base: pass --storage-base, or set HOME"))
}

/// Exclusive advisory lock on `<kernel dir>/.lock`; released when the file is dropped.
fn take_lock(kernel_dir: &Path) -> fs::File {
    let path = kernel_dir.join(".lock");
    let f = fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(&path)
        .unwrap_or_else(|e| internal(&format!("open {}: {e}", path.display())));
    f.lock().unwrap_or_else(|e| internal(&format!("lock {}: {e}", path.display())));
    f
}

fn load_index(kernel_dir: &Path, kernel_id: &str) -> Index {
    let path = kernel_dir.join(INDEX_FILE);
    match fs::read_to_string(&path) {
        Ok(text) => {
            let idx: Index = toml::from_str(&text).unwrap_or_else(|e| {
                internal(&format!("{} cannot be parsed ({e}); fix it, or run --rebuild-index", path.display()))
            });
            if idx.schema != SCHEMA {
                internal(&format!("{} has schema {}; this script reads schema {SCHEMA}", path.display(), idx.schema));
            }
            if idx.kernel_id != kernel_id {
                internal(&format!("{} belongs to kernel-id {:?}", path.display(), idx.kernel_id));
            }
            idx
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            if let Some(d) = subdirs(kernel_dir).first() {
                internal(&format!(
                    "{} is missing but captures exist (for example {}); run --rebuild-index",
                    path.display(),
                    d.display()
                ));
            }
            Index { schema: SCHEMA, kernel_id: kernel_id.into(), profile: vec![], version: vec![], capture: vec![] }
        }
        Err(e) => internal(&format!("read {}: {e}", path.display())),
    }
}

/// Writes the index to a temp file and renames it into place.
fn save_index(kernel_dir: &Path, idx: &Index) {
    let path = kernel_dir.join(INDEX_FILE);
    let tmp = kernel_dir.join(format!(".{INDEX_FILE}.tmp-{}", std::process::id()));
    let text = format!(
        "# Written by capture_att_trace.rs. This index wins over the captures' {META_FILE} files.\n{}",
        toml::to_string(idx).unwrap_or_else(|e| internal(&format!("serialize index: {e}")))
    );
    let write = || -> std::io::Result<()> {
        let mut f = fs::File::create(&tmp)?;
        f.write_all(text.as_bytes())?;
        f.sync_all()?;
        fs::rename(&tmp, &path)
    };
    write().unwrap_or_else(|e| internal(&format!("write {}: {e}", path.display())));
}

/// The capture directories of the kernel directory: its subdirectories not starting with `.`.
fn subdirs(kernel_dir: &Path) -> Vec<PathBuf> {
    let Ok(rd) = fs::read_dir(kernel_dir) else { return Vec::new() };
    let mut v: Vec<PathBuf> = rd
        .flatten()
        .filter(|e| e.file_type().is_ok_and(|t| t.is_dir()) && !e.file_name().to_string_lossy().starts_with('.'))
        .map(|e| e.path())
        .collect();
    v.sort();
    v
}

/// The capture directories that hold an att_capture.toml.
fn capture_dirs(kernel_dir: &Path) -> Vec<PathBuf> {
    subdirs(kernel_dir).into_iter().filter(|p| p.join(META_FILE).is_file()).collect()
}

/// Checks the version against the index and returns its (profile, rep). Exits on a conflict.
fn plan(idx: &Index, a: &Args) -> (u64, u64) {
    let version = a.version.as_deref().unwrap();
    let mut errs = Errors::default();
    if let Some(v) = idx.version.iter().find(|v| v.name == version) {
        if v.desc != a.desc {
            errs.add(
                "--desc",
                format!(
                    "{version} of this kernel is recorded with desc {:?}; reuse that desc, or bump --version",
                    v.desc.as_deref().unwrap_or("")
                ),
            );
        }
        if v.command != a.command {
            errs.add(
                "--version",
                format!(
                    "{version} is recorded with test command `{}`; a test-command change bumps the version",
                    shell_join(&v.command)
                ),
            );
        }
    }
    if !errs.0.is_empty() {
        fail_options(&errs);
    }
    let profile = match idx.profile.iter().find(|p| p.rocprofv3_options == a.rocprof_opts) {
        Some(p) => p.id,
        None => idx.profile.iter().map(|p| p.id + 1).max().unwrap_or(0),
    };
    let rep = idx
        .capture
        .iter()
        .filter(|c| c.version == version && c.profile == profile)
        .map(|c| c.rep + 1)
        .max()
        .unwrap_or(1);
    (profile, rep)
}

fn capture_name(stamp: &str, a: &Args, profile: u64, rep: u64) -> String {
    let mut t = format!("{stamp}-{}-{}", a.kernel_id.as_deref().unwrap(), a.version.as_deref().unwrap());
    if let Some(d) = &a.desc {
        t += &format!("-{d}");
    }
    if profile > 0 {
        t += &format!("-prof{profile}");
    }
    if rep > 1 {
        t += &format!("-rep{rep}");
    }
    t
}

/// Compares this capture's per-kernel machine-code hashes with those recorded for its version,
/// merges them into the version when `record` is set, and collects consistency problems. Only
/// kernels both sides hashed are compared, so captures whose filters select different kernels do
/// not conflict. A kernel's hashes match when one set contains the other (see `nested`). Returns
/// whether the hashes were merged.
fn check_isa(
    idx: &mut Index,
    a: &Args,
    dir_name: &str,
    isa: &IsaResult,
    record: bool,
    errors: &mut Vec<String>,
    warnings: &mut Vec<String>,
) -> bool {
    let version = a.version.clone().unwrap();
    if !idx.capture.iter().any(|c| c.dir == dir_name) {
        internal(&format!("{dir_name} disappeared from {INDEX_FILE} during the capture"));
    }
    let Some(hash) = isa.hash.as_deref() else { return false };
    let mine = &isa.by_kernel;
    let mut recorded = false;
    if let Some(v) = idx.version.iter_mut().find(|v| v.name == version) {
        if v.isa_kernels.is_empty() && v.isa_hash.as_deref().is_some_and(|prev| prev != hash) {
            // An index written before per-kernel hashes: a differing combined hash can also come
            // from a different kernel selection, so it is not an error.
            warnings.push(format!(
                "{version} is recorded with the combined isa-hash {}, from before per-kernel hashes, and this capture's \
                 {hash} differs; to compare per kernel, run --rebuild-index --kernel-id {}",
                v.isa_hash.as_deref().unwrap_or_default(),
                idx.kernel_id
            ));
        } else {
            let changed: Vec<&str> = mine
                .iter()
                .filter(|(k, h)| v.isa_kernels.get(*k).is_some_and(|r| !nested(r, h)))
                .map(|(k, _)| k.as_str())
                .collect();
            if !changed.is_empty() {
                errors.push(format!(
                    "the machine code of {} differs from earlier captures of {version}; the kernel code changed, \
                     so capture again with a new --version",
                    name_list(&changed)
                ));
            } else if record {
                for (k, h) in mine {
                    merge_hashes(&mut v.isa_kernels, k, h);
                }
                recorded = true;
            }
        }
    }
    for o in idx.version.iter().filter(|o| o.name != version && o.command == a.command) {
        let same = if o.isa_kernels.is_empty() {
            o.isa_hash.as_deref() == Some(hash)
        } else {
            mine.iter().all(|(k, h)| o.isa_kernels.get(k).is_some_and(|r| nested(r, h)))
        };
        if same {
            warnings.push(format!(
                "{} has the same machine code for {} and the same test command as {version}; nothing changed between them",
                o.name,
                name_list(&mine.keys().map(String::as_str).collect::<Vec<_>>())
            ));
        }
    }
    recorded
}

/// Whether one set of a kernel's hashes contains the other. A JIT kernel can load a different
/// number of variants per run (Triton autotune with cached results loads only the winner), so a
/// subset is the same code; a hash on each side that the other lacks is a code change.
fn nested(a: &[String], b: &[String]) -> bool {
    a.iter().all(|h| b.contains(h)) || b.iter().all(|h| a.contains(h))
}

/// Adds `hashes` to the kernel's recorded hashes, kept sorted and unique.
fn merge_hashes(map: &mut BTreeMap<String, Vec<String>>, kernel: &str, hashes: &[String]) {
    let e = map.entry(kernel.to_string()).or_default();
    e.extend(hashes.iter().cloned());
    e.sort();
    e.dedup();
}

/// Up to three kernel names, then how many more.
fn name_list(names: &[&str]) -> String {
    let mut s = names.iter().take(3).map(|n| format!("`{n}`")).collect::<Vec<_>>().join(", ");
    if names.len() > 3 {
        s += &format!(" and {} more", names.len() - 3);
    }
    s
}

#[derive(Default)]
struct IsaResult {
    hash: Option<String>,
    kernels: Vec<Value>,
    /// Sorted `sha256:` hashes by demangled kernel name.
    by_kernel: BTreeMap<String, Vec<String>>,
    warnings: Vec<String>,
}

/// Hashes the machine code of the kernels that `--kernel-include-regex` selects, read from the
/// code objects rocprofv3 dumps as `*_code_object_id_*.out`. The regexes are matched against the
/// name rocprofv3 matches them against (see `filter_name`); the hash input keeps the demangled name.
fn compute_isa_hash(dir: &Path, opts: &[String]) -> IsaResult {
    let (mangled, truncate) = naming_flags(opts);
    let mut r = IsaResult::default();
    let skip = |r: &mut IsaResult, why: String| r.warnings.push(format!("isa-hash not computed: {why}"));
    let Some(include) = opt_value(opts, "--kernel-include-regex") else {
        skip(&mut r, "no --kernel-include-regex among the rocprofv3 options, so the traced kernel is unknown".into());
        return r;
    };
    let include = match Regex::new(&include) {
        Ok(re) => re,
        Err(e) => {
            skip(&mut r, format!("--kernel-include-regex: {e}"));
            return r;
        }
    };
    let exclude = match opt_value(opts, "--kernel-exclude-regex").map(|s| Regex::new(&s)).transpose() {
        Ok(re) => re,
        Err(e) => {
            skip(&mut r, format!("--kernel-exclude-regex: {e}"));
            return r;
        }
    };
    let mut files = Vec::new();
    find_code_objects(dir, &mut files);
    if files.is_empty() {
        skip(&mut r, "rocprofv3 dumped no *_code_object_id_*.out file".into());
        return r;
    }
    let mut found: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for f in &files {
        let Ok(data) = fs::read(f) else { continue };
        let Ok(obj) = object::File::parse(&*data) else { continue };
        let mut syms: Vec<_> = obj.symbols().collect();
        if syms.is_empty() {
            syms = obj.dynamic_symbols().collect();
        }
        let names: HashSet<&str> = syms.iter().filter_map(|s| s.name().ok()).collect();
        for s in &syms {
            let Ok(name) = s.name() else { continue };
            // A kernel is a function symbol with a `<name>.kd` kernel descriptor beside it.
            if s.kind() != SymbolKind::Text || s.size() == 0 || !names.contains(format!("{name}.kd").as_str()) {
                continue;
            }
            let pretty = demangle(name);
            let seen = filter_name(name, &pretty, mangled, truncate);
            if !include.is_match(&seen) || exclude.as_ref().is_some_and(|e| e.is_match(&seen)) {
                continue;
            }
            let Some(sec) = s.section_index().and_then(|i| obj.section_by_index(i).ok()) else { continue };
            let Ok(Some(bytes)) = sec.data_range(s.address(), s.size()) else { continue };
            found.entry(pretty).or_default().insert(sha256_hex(bytes));
        }
    }
    if found.is_empty() {
        skip(&mut r, "no kernel in the dumped code objects matches --kernel-include-regex".into());
        return r;
    }
    let mut lines = String::new();
    for (name, hashes) in &found {
        for h in hashes {
            lines += &format!("{name}\t{h}\n");
            r.kernels.push(json!({"name": name, "hash": format!("sha256:{h}")}));
        }
        r.by_kernel.insert(name.clone(), hashes.iter().map(|h| format!("sha256:{h}")).collect());
    }
    r.hash = Some(format!("sha256:{}", sha256_hex(lines.as_bytes())));
    r
}

/// The demangled kernel symbol, or the symbol itself when it is not a C++ mangled name.
fn demangle(sym: &str) -> String {
    cpp_demangle::Symbol::new(sym.as_bytes()).ok().and_then(|d| d.demangle().ok()).unwrap_or_else(|| sym.to_string())
}

/// The name rocprofv3's kernel filters see for the kernel symbol `sym` (rocprofiler-sdk
/// `tool::format_name`): with only `-M`, the raw descriptor symbol `<sym>.kd`; otherwise the
/// demangled name, truncated with `-T`, which overrides `-M`.
fn filter_name(sym: &str, demangled: &str, mangled: bool, truncate: bool) -> String {
    if truncate {
        truncate_name(demangled)
    } else if mangled {
        format!("{sym}.kd")
    } else {
        demangled.to_string()
    }
}

/// Port of rocprofiler-sdk `common::truncate_name`: from the end, skip spaces and balanced
/// `(...)`, `<...>` and `[...]` groups, then keep the identifier back to the last space or `:`.
/// `void ns::f<int>(int*)` becomes `f`.
fn truncate_name(name: &str) -> String {
    let b = name.as_bytes();
    let mut i = b.len();
    let (mut depth, mut open, mut close) = (0u32, 0u8, 0u8);
    while i > 0 {
        let c = b[i - 1];
        if depth == 0 {
            (open, close) = match c {
                b')' => (b')', b'('),
                b'>' => (b'>', b'<'),
                b']' => (b']', b'['),
                b' ' => {
                    i -= 1;
                    continue;
                }
                _ => break,
            };
            depth = 1;
        } else if c == open {
            depth += 1;
        } else if c == close {
            depth -= 1;
        }
        i -= 1;
    }
    let end = i;
    while i > 0 && b[i - 1] != b' ' && b[i - 1] != b':' {
        i -= 1;
    }
    String::from_utf8_lossy(&b[i..end]).into_owned()
}

/// `-M`/`--mangled-kernels` and `-T`/`--truncate-kernels` among the rocprofv3 options, each with
/// an optional BOOL (rocprofv3's `strtobool`); the last one given wins.
fn naming_flags(opts: &[String]) -> (bool, bool) {
    let on = |names: &[&str]| bool_option(opts, names).unwrap_or(false);
    (on(&["-M", "--mangled-kernels"]), on(&["-T", "--truncate-kernels"]))
}

/// The value of the last of `names`, rocprofv3 options with an optional BOOL that read alone as
/// true, or `None` when none is given.
fn bool_option(opts: &[String], names: &[&str]) -> Option<bool> {
    let wanted: Vec<(&str, Arity)> = names.iter().map(|n| (*n, Arity::OptionalBool)).collect();
    let (toks, _) = split_options(opts, &wanted);
    let mut on = None;
    let mut it = toks.iter().peekable();
    while let Some(t) = it.next() {
        let value = t.split_once('=').map(|(_, v)| v.to_string()).or_else(|| it.next_if(|v| !v.starts_with('-')).cloned());
        on = Some(value.is_none_or(|v| matches!(v.to_ascii_lowercase().as_str(), "y" | "yes" | "t" | "true" | "on" | "1")));
    }
    on
}

/// The value of the last `FLAG VALUE` or `FLAG=VALUE` among the tokens.
fn opt_value(opts: &[String], flag: &str) -> Option<String> {
    let mut v = None;
    let mut it = opts.iter();
    while let Some(t) = it.next() {
        if t == flag {
            v = it.next().cloned();
        } else if let Some(rest) = t.strip_prefix(flag).and_then(|r| r.strip_prefix('=')) {
            v = Some(rest.to_string());
        }
    }
    v
}

fn find_code_objects(dir: &Path, out: &mut Vec<PathBuf>) {
    find_files(dir, &|name| name.contains("_code_object_id_") && name.ends_with(".out"), out);
}

/// The files under `dir`, recursively, whose name `keep` accepts.
fn find_files(dir: &Path, keep: &dyn Fn(&str) -> bool, out: &mut Vec<PathBuf>) {
    let Ok(rd) = fs::read_dir(dir) else { return };
    for e in rd.flatten() {
        let p = e.path();
        let name = e.file_name().to_string_lossy().into_owned();
        match e.file_type() {
            Ok(ft) if ft.is_dir() => find_files(&p, keep, out),
            Ok(_) if keep(&name) => out.push(p),
            _ => {}
        }
    }
    out.sort();
}

fn sha256_hex(bytes: &[u8]) -> String {
    Sha256::digest(bytes).iter().map(|b| format!("{b:02x}")).collect()
}

/// Rebuilds `<kernel-id>/index.toml` from the captures' att_capture.toml files.
fn rebuild_index(a: &Args) {
    let kid = a.kernel_id.as_deref().unwrap();
    let kernel_dir = resolve_storage_base(a).join(kid);
    if !kernel_dir.is_dir() {
        internal(&format!("{} does not exist", kernel_dir.display()));
    }
    let _lock = take_lock(&kernel_dir);
    let mut problems = Vec::new();
    let mut profiles: BTreeMap<u64, Vec<String>> = BTreeMap::new();
    let mut versions: Vec<VersionEntry> = Vec::new();
    let mut captures: Vec<CaptureEntry> = Vec::new();
    let mut seen: BTreeMap<(String, u64, u64), String> = BTreeMap::new();
    // The old index, if readable: the evidence for captures their att_capture.toml cannot rebuild.
    let old: Option<Index> = fs::read_to_string(kernel_dir.join(INDEX_FILE))
        .ok()
        .and_then(|t| toml::from_str::<Index>(&t).ok())
        .filter(|i| i.kernel_id == kid);

    for d in capture_dirs(&kernel_dir) {
        let name = d.file_name().unwrap().to_string_lossy().into_owned();
        let path = d.join(META_FILE);
        let m = match fs::read_to_string(&path).map_err(|e| e.to_string()).and_then(|t| toml::from_str::<toml::Table>(&t).map_err(|e| e.to_string())) {
            Ok(t) => toml_to_json(&toml::Value::Table(t)),
            Err(e) => {
                problems.push(format!("{}: {e}", path.display()));
                continue;
            }
        };
        let strs = |v: &Value| -> Option<Vec<String>> {
            v.as_array()?.iter().map(|x| x.as_str().map(String::from)).collect()
        };
        let fields = (
            m["structured"].as_bool(),
            m["kernel-id"].as_str(),
            m["version"].as_str(),
            m["profile"].as_u64(),
            m["rep"].as_u64(),
            strs(&m["rocprofv3-options"]),
            strs(&m["command"]),
            m["started"].as_str().and_then(|s| s.parse::<toml::value::Datetime>().ok()),
        );
        let (Some(true), Some(k), Some(version), Some(profile), Some(rep), Some(opts), Some(command), Some(started)) = fields else {
            problems.push(format!("{}: not a structured capture, or a field is missing", path.display()));
            continue;
        };
        if k != kid {
            problems.push(format!("{}: kernel-id is {k:?}", path.display()));
            continue;
        }
        let desc = m["desc"].as_str().map(String::from);
        let isa_hash = m["isa-hash"].as_str().map(String::from);
        // Per-kernel hashes come only from the captures that merged them when they ran.
        let mut isa_kernels: BTreeMap<String, Vec<String>> = BTreeMap::new();
        if isa_recorded(&m["result"]) {
            for k in m["result"]["isa-kernels"].as_array().into_iter().flatten() {
                if let (Some(n), Some(h)) = (k["name"].as_str(), k["hash"].as_str()) {
                    merge_hashes(&mut isa_kernels, n, &[h.to_string()]);
                }
            }
        }

        rebuild_profile(&mut profiles, profile, &opts, &name, &mut problems);
        let declared = VersionEntry { name: version.into(), desc, command, isa_hash: None, isa_kernels };
        // Each recorded capture passed the consistency check when it finished, against the hashes
        // accepted by then. Finish order is not directory order, so merge without re-checking.
        rebuild_version(&mut versions, declared, false, &name, &mut problems);
        // Metadata without `result` is still in its initial state: the run stopped before its final
        // att_capture.toml write, which follows the index's. If the old index records the capture
        // as finished, its entry holds the verdict and the version's accepted hashes, so keep that
        // entry (below) rather than a PENDING one without hashes.
        let finished_in_old =
            old.as_ref().is_some_and(|o| o.capture.iter().any(|c| c.dir == name && c.verdict != "PENDING"));
        if m["result"].is_null() && finished_in_old {
            continue;
        }
        if let Some(other) = seen.insert((version.into(), profile, rep), name.clone()) {
            problems.push(format!("{name} and {other} both claim {version} prof{profile} rep{rep}"));
        }
        captures.push(CaptureEntry {
            dir: name,
            version: version.into(),
            profile,
            rep,
            started,
            verdict: m["result"]["verdict"].as_str().unwrap_or("PENDING").into(),
            isa_hash,
            note: m["note"].as_str().map(String::from),
        });
    }

    // A capture the old index records but no att_capture.toml rebuilds (interrupted before the file
    // or its directory was written, or before its final write, or with either lost since) keeps its
    // entry, so its numbers stay reserved. Its profile and version declarations face the same checks
    // as rebuilt ones.
    let rebuilt: HashSet<String> = captures.iter().map(|c| c.dir.clone()).collect();
    for (o, c) in old.iter().flat_map(|o| o.capture.iter().map(move |c| (o, c))) {
        if rebuilt.contains(&c.dir) {
            continue;
        }
        let who = format!("{} (kept from the old {INDEX_FILE})", c.dir);
        match o.profile.iter().find(|p| p.id == c.profile) {
            Some(p) => rebuild_profile(&mut profiles, p.id, &p.rocprofv3_options, &who, &mut problems),
            None => problems.push(format!("{who}: the old {INDEX_FILE} does not declare its profile {}", c.profile)),
        }
        match o.version.iter().find(|v| v.name == c.version) {
            // The old index merges hashes per version, not per capture, so the kept entry's own
            // contribution is unknown: keep all of the version's accepted hashes, or changed code
            // could reuse the version unnoticed.
            Some(v) => rebuild_version(&mut versions, v.clone(), true, &who, &mut problems),
            None => problems.push(format!("{who}: the old {INDEX_FILE} does not declare its version {}", c.version)),
        }
        if let Some(other) = seen.insert((c.version.clone(), c.profile, c.rep), c.dir.clone()) {
            problems.push(format!("{} and {other} both claim {} prof{} rep{}", c.dir, c.version, c.profile, c.rep));
        }
        captures.push(c.clone());
    }
    // A directory that neither an att_capture.toml nor the old index identifies stops the rebuild.
    for d in subdirs(&kernel_dir) {
        let name = d.file_name().unwrap().to_string_lossy().into_owned();
        if !captures.iter().any(|c| c.dir == name) {
            problems.push(unknown_dir_problem(&d, &kernel_dir));
        }
    }
    captures.sort_by(|x, y| x.dir.cmp(&y.dir));

    if !problems.is_empty() {
        let out = json!({"error": {"options": ["--rebuild-index"], "messages": {"--rebuild-index": problems}}});
        print_json(&out);
        std::process::exit(1);
    }

    let path = kernel_dir.join(INDEX_FILE);
    let backup = path.exists().then(|| backup_index(&kernel_dir, &path));
    let idx = Index {
        schema: SCHEMA,
        kernel_id: kid.into(),
        profile: profiles.into_iter().map(|(id, o)| ProfileEntry { id, rocprofv3_options: o }).collect(),
        version: versions,
        capture: captures,
    };
    save_index(&kernel_dir, &idx);
    print_json(&json!({
        "index": path.display().to_string(),
        "backup": backup,
        "profiles": idx.profile.len(),
        "versions": idx.version.len(),
        "captures": idx.capture.len(),
    }));
}

/// Adds a capture's profile to the rebuilt ones; options that disagree with another capture's are a
/// problem.
fn rebuild_profile(profiles: &mut BTreeMap<u64, Vec<String>>, id: u64, opts: &[String], who: &str, problems: &mut Vec<String>) {
    match profiles.get(&id) {
        Some(o) if o != opts => {
            problems.push(format!("{who}: profile {id} has different rocprofv3 options than another capture"))
        }
        Some(_) => {}
        None => {
            if let Some((other, _)) = profiles.iter().find(|(_, o)| o.as_slice() == opts) {
                problems.push(format!("{who}: its rocprofv3 options are profile {other} elsewhere, but it says profile {id}"));
            }
            profiles.insert(id, opts.to_vec());
        }
    }
}

/// Adds a version declaration and its hashes to the rebuilt ones. A desc or test command that
/// disagrees with another capture's is a problem. With `check_hashes`, so are a kernel's hashes
/// when neither they nor the hashes rebuilt so far contain the other, as in a capture's
/// consistency check; callers pass it only once every capture's hashes are merged, so the result
/// does not depend on the order captures are read in.
fn rebuild_version(
    versions: &mut Vec<VersionEntry>,
    declared: VersionEntry,
    check_hashes: bool,
    who: &str,
    problems: &mut Vec<String>,
) {
    match versions.iter_mut().find(|v| v.name == declared.name) {
        Some(v) => {
            if v.desc != declared.desc || v.command != declared.command {
                problems.push(format!(
                    "{who}: {} has a different desc or test command than another capture",
                    declared.name
                ));
            }
            for (k, h) in &declared.isa_kernels {
                if check_hashes && v.isa_kernels.get(k).is_some_and(|r| !nested(r, h)) {
                    problems.push(format!(
                        "{who}: {} records different machine code for `{k}` than another capture",
                        declared.name
                    ));
                } else {
                    merge_hashes(&mut v.isa_kernels, k, h);
                }
            }
            if v.isa_hash.is_none() {
                v.isa_hash = declared.isa_hash;
            }
        }
        None => versions.push(declared),
    }
}

/// Why a capture directory that nothing identifies stops the rebuild, with what it holds: the
/// script cannot tell a capture interrupted before rocprofv3 ran from one whose metadata was lost.
fn unknown_dir_problem(d: &Path, kernel_dir: &Path) -> String {
    let (mut ui, mut stats, mut att) = (Vec::new(), Vec::new(), 0u64);
    walk(d, d, &mut ui, &mut stats, &mut att);
    let entries = fs::read_dir(d).map(|r| r.count()).unwrap_or(0);
    let holds = if entries == 0 {
        "It is empty".to_string()
    } else {
        format!(
            "It holds {entries} entries, with {att} .att files and {} ui_output directories, and may hold a trace",
            ui.len()
        )
    };
    format!(
        "{}: no {META_FILE}, and no readable {INDEX_FILE} records it, so its version, profile and rep cannot be \
         recovered automatically. {holds}. Inspect it; to rebuild without it, move it out of {} (its numbers \
         may then be reused), then run --rebuild-index again",
        d.display(),
        kernel_dir.display()
    )
}

/// Whether a capture merged its hashes into its version: its `result.isa-recorded`, or, for a
/// capture written before that field, a completed run with no consistency errors.
fn isa_recorded(result: &Value) -> bool {
    if let Some(b) = result["isa-recorded"].as_bool() {
        return b;
    }
    let verdict = result["verdict"].as_str().unwrap_or_default();
    result["rocprofv3-exit-code"].as_i64() == Some(0)
        && result["consistency-errors"].as_array().is_none_or(|e| e.is_empty())
        && !verdict.starts_with("INCONSISTENT")
        && verdict != FATAL_VERDICT
}

/// Copies the index to a new `index.toml.bak-<timestamp>[-<n>]`, never over an existing backup.
fn backup_index(kernel_dir: &Path, path: &Path) -> String {
    let text = fs::read(path).unwrap_or_else(|e| internal(&format!("read {}: {e}", path.display())));
    let stamp = utc_now().0;
    for n in 1u32.. {
        let suffix = if n == 1 { String::new() } else { format!("-{n}") };
        let b = kernel_dir.join(format!("{INDEX_FILE}.bak-{stamp}{suffix}"));
        match fs::OpenOptions::new().write(true).create_new(true).open(&b) {
            Ok(mut f) => {
                f.write_all(&text)
                    .and_then(|()| f.sync_all())
                    .unwrap_or_else(|e| internal(&format!("write {}: {e}", b.display())));
                return b.display().to_string();
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(e) => internal(&format!("create {}: {e}", b.display())),
        }
    }
    unreachable!()
}

/// Dispatches of one kernel name in `--list-kernels` and `--list-filtered-kernels`.
#[derive(Default)]
struct KernelDispatches {
    total_ns: u64,
    /// Sorted dispatch ids per (process, Kernel_Id): `--kernel-iteration-range` counts each loaded
    /// kernel symbol separately. A process is one `<pid>_kernel_trace.csv` file.
    per_symbol: BTreeMap<(String, u64), Vec<u64>>,
}

/// One rocprofv3 run of `--list-kernels` or `--list-filtered-kernels`.
struct Listing {
    command: Vec<String>,
    dir: PathBuf,
    run: RunOutcome,
}

/// How long the profiled command may live after rocprofv3 prints a fatal error: after a failed
/// profiling setup, it can hang in its signal handler instead of exiting.
const FATAL_GRACE: Duration = Duration::from_secs(30);
const KILLED_VERDICT: &str = "FAILED: rocprofv3 killed after a fatal error";

/// The counter `--list-filtered-kernels` collects so that rocprofv3 applies its own kernel
/// filters. A constant from the agent properties (`simd_count/simd_per_cu`), it needs no hardware
/// counter packets. Unlike `MAX_WAVE_SIZE`, rocprofiler-sdk defines it for gfx1250 as well as gfx9.
const SELECTION_COUNTER: &str = "CU_NUM";

#[derive(Clone, Copy)]
enum Arity {
    OptionalBool,
    One,
    OneOrMore,
}

const NAMING_OPTS: [(&str, Arity); 4] = [
    ("-M", Arity::OptionalBool),
    ("--mangled-kernels", Arity::OptionalBool),
    ("-T", Arity::OptionalBool),
    ("--truncate-kernels", Arity::OptionalBool),
];

const FILTER_OPTS: [(&str, Arity); 3] = [
    ("--kernel-include-regex", Arity::One),
    ("--kernel-exclude-regex", Arity::One),
    ("--kernel-iteration-range", Arity::OneOrMore),
];

/// `--list-kernels`: runs the command under `rocprofv3 --kernel-trace` in a new temporary
/// directory and prints the kernels it dispatched, named as the kernel filters see them.
/// `--list-filtered-kernels` also collects `SELECTION_COUNTER` with the kernel filters, so that
/// the dispatches with a counter record are exactly those rocprofv3's filters select; if that
/// run fails, the selection is approximated from a plain kernel trace.
fn list_kernels(a: &Args) -> ! {
    let filtering = a.list_filtered_kernels;
    let (naming, _) = split_options(&a.rocprof_opts, &NAMING_OPTS);
    let (filters, _) = split_options(&a.rocprof_opts, &FILTER_OPTS);
    let kept: Vec<(&str, Arity)> =
        NAMING_OPTS.iter().chain(if filtering { &FILTER_OPTS[..] } else { &[] }).copied().collect();
    let (_, ignored) = split_options(&a.rocprof_opts, &kept);
    let range = if filtering { range_spec(&a.rocprof_opts) } else { None };

    let (stamp, _) = utc_now();
    let base = std::env::temp_dir().join(format!("capture_att_trace-list-kernels-{stamp}-{}", std::process::id()));
    fs::create_dir(&base).unwrap_or_else(|e| internal(&format!("create {}: {e}", base.display())));

    let mut warnings: Vec<String> = Vec::new();
    let mut source = None;
    let mut pmc_run = None;
    if filtering {
        let mut opts = naming.clone();
        opts.extend(["--pmc".to_string(), SELECTION_COUNTER.to_string()]);
        opts.extend(filters.iter().cloned());
        if range.is_none() {
            // ATT without a range traces each kernel symbol's 1st dispatch; PMC would count all.
            opts.extend(["--kernel-iteration-range".to_string(), "1".to_string()]);
        }
        let r = run_listing(&base, "pmc", &opts, &a.command);
        match r.run.failure() {
            None => {
                source = Some("rocprofv3");
                pmc_run = Some(r);
            }
            Some((verdict, _)) => warnings.push(format!(
                "rocprofv3 --pmc {SELECTION_COUNTER} ended with {verdict} (exit code {}), so the selection is \
                 approximated; see {}",
                r.run.rc,
                r.dir.join("rocprofv3.log").display()
            )),
        }
    }
    let run = match pmc_run {
        Some(r) => r,
        None => {
            if filtering {
                source = Some("approximation");
            }
            run_listing(&base, "trace", &naming, &a.command)
        }
    };

    let kernels = read_kernel_traces(&run.dir);
    let mut filter_errors = Vec::new();
    let selected: Option<BTreeSet<(String, u64)>> = match source {
        Some("rocprofv3") => Some(read_counted_dispatches(&run.dir)),
        Some(_) => {
            let mut compile = |opt: &str| match opt_value(&a.rocprof_opts, opt) {
                None => Ok(None),
                Some(re) => Regex::new(&re).map(Some).map_err(|e| filter_errors.push(format!("{opt}: {e}"))),
            };
            let (inc, exc) = (compile("--kernel-include-regex"), compile("--kernel-exclude-regex"));
            let iterations = range.as_deref().map_or_else(|| Ok(BTreeSet::from([1])), parse_range);
            Some(match (inc, exc, iterations, &kernels) {
                (Ok(inc), Ok(exc), Ok(its), Some(k)) => approximate_selection(k, inc.as_ref(), exc.as_ref(), &its),
                (_, _, Err(e), _) => {
                    filter_errors.push(format!("--kernel-iteration-range: {e}"));
                    BTreeSet::new()
                }
                _ => BTreeSet::new(),
            })
        }
        None => None,
    };

    let empty = BTreeMap::new();
    let kmap = kernels.as_ref().unwrap_or(&empty);
    let total_ns: u64 = kmap.values().map(|k| k.total_ns).sum();
    let mut rows: Vec<(&String, &KernelDispatches)> = kmap.iter().collect();
    rows.sort_by(|x, y| y.1.total_ns.cmp(&x.1.total_ns).then(x.0.cmp(y.0)));
    let (mut sel_kernels, mut sel_dispatches) = (0u64, 0u64);
    let list: Vec<Value> = rows
        .iter()
        .map(|(name, k)| {
            let mut v = json!({
                "name": name,
                "dispatches": k.per_symbol.values().map(Vec::len).sum::<usize>(),
                "total-ns": k.total_ns,
                "percent": if total_ns == 0 { 0.0 } else { (k.total_ns as f64 * 1000.0 / total_ns as f64).round() / 10.0 },
                "dispatches-per-symbol": k.per_symbol.values().map(Vec::len).collect::<Vec<_>>(),
            });
            if let Some(sel) = &selected {
                let mut ids: Vec<u64> = k
                    .per_symbol
                    .iter()
                    .flat_map(|((p, _), ids)| ids.iter().filter(|d| sel.contains(&(p.clone(), **d))).copied())
                    .collect();
                ids.sort_unstable();
                sel_kernels += u64::from(!ids.is_empty());
                sel_dispatches += ids.len() as u64;
                v["selected"] = json!(!ids.is_empty());
                v["selected-dispatch-ids"] = json!(ids);
            }
            v
        })
        .collect();

    let (verdict, exit_code) = if let Some(failure) = run.run.failure() {
        failure
    } else if kernels.is_none() {
        ("FAILED: no kernel_trace.csv", 3)
    } else if !warnings.is_empty() {
        ("OK WITH WARNINGS", 0)
    } else {
        ("OK", 0)
    };
    let mut out = json!({
        "rocprofv3-command": run.command,
        "ignored-rocprofv3-options": ignored,
        "rocprofv3-exit-code": run.run.rc,
        "kernels": list,
    });
    if let Some(src) = source {
        out["selection-source"] = json!(src);
        out["kernel-iteration-range"] = json!(range.unwrap_or_else(|| "1 (the ATT default)".into()));
        out["selected-kernels"] = json!(sel_kernels);
        out["selected-dispatches"] = json!(sel_dispatches);
        if src == "approximation" {
            out["filter-errors"] = json!(filter_errors);
        }
    }
    out["warnings"] = json!(warnings);
    if verdict == "OK" {
        let _ = fs::remove_dir_all(&base);
    } else {
        out["output-dir"] = json!(base.display().to_string());
    }
    out["verdict"] = json!(verdict);
    out["exit-code"] = json!(exit_code);
    print_json(&out);
    std::process::exit(exit_code);
}

/// Runs `rocprofv3 --kernel-trace --output-format csv -d <base>/<name> OPTS -- COMMAND`.
fn run_listing(base: &Path, name: &str, opts: &[String], command: &[String]) -> Listing {
    let dir = base.join(name);
    fs::create_dir(&dir).unwrap_or_else(|e| internal(&format!("create {}: {e}", dir.display())));
    let mut full: Vec<String> = ["rocprofv3", "--kernel-trace", "--output-format", "csv", "-d"].map(String::from).to_vec();
    full.push(dir.display().to_string());
    full.extend(opts.iter().cloned());
    full.push("--".into());
    full.extend(command.iter().cloned());
    eprintln!("list-kernels: {}", shell_join(&full));
    let run = run_logged(&full, &dir.join("rocprofv3.log"));
    Listing { command: full, dir, run }
}

/// A process key for `<dir>/<pid>_<suffix>`, shared by that process's CSV files.
fn process_key(file: &Path, suffix: &str) -> String {
    let name = file.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    let parent = file.parent().map(|p| p.display().to_string()).unwrap_or_default();
    format!("{parent}/{}", name.strip_suffix(suffix).unwrap_or(&name))
}

/// The `*kernel_trace.csv` files under `dir`, by kernel name; `None` when there is none.
fn read_kernel_traces(dir: &Path) -> Option<BTreeMap<String, KernelDispatches>> {
    let mut files = Vec::new();
    find_files(dir, &|n| n.ends_with("kernel_trace.csv"), &mut files);
    if files.is_empty() {
        return None;
    }
    let mut kernels: BTreeMap<String, KernelDispatches> = BTreeMap::new();
    for f in &files {
        let proc = process_key(f, "kernel_trace.csv");
        let Ok(mut rdr) = csv::Reader::from_path(f) else { continue };
        let Ok(headers) = rdr.headers().cloned() else { continue };
        let col = |n: &str| headers.iter().position(|h| h == n);
        let (Some(name_i), Some(kid_i), Some(did_i), Some(start_i), Some(end_i)) = (
            col("Kernel_Name"),
            col("Kernel_Id"),
            col("Dispatch_Id"),
            col("Start_Timestamp"),
            col("End_Timestamp"),
        ) else {
            continue;
        };
        for rec in rdr.records().flatten() {
            let num = |i: usize| rec.get(i).and_then(|s| s.trim().parse::<u64>().ok()).unwrap_or(0);
            let k = kernels.entry(rec.get(name_i).unwrap_or_default().to_string()).or_default();
            k.total_ns += num(end_i).saturating_sub(num(start_i));
            k.per_symbol.entry((proc.clone(), num(kid_i))).or_default().push(num(did_i));
        }
    }
    for k in kernels.values_mut() {
        k.per_symbol.values_mut().for_each(|ids| ids.sort_unstable());
    }
    Some(kernels)
}

/// The (process, Dispatch_Id) pairs in the `*counter_collection.csv` files under `dir`: the
/// dispatches rocprofv3's kernel filters selected.
fn read_counted_dispatches(dir: &Path) -> BTreeSet<(String, u64)> {
    let mut files = Vec::new();
    find_files(dir, &|n| n.ends_with("counter_collection.csv"), &mut files);
    let mut out = BTreeSet::new();
    for f in &files {
        let proc = process_key(f, "counter_collection.csv");
        let Ok(mut rdr) = csv::Reader::from_path(f) else { continue };
        let Ok(headers) = rdr.headers().cloned() else { continue };
        let Some(did_i) = headers.iter().position(|h| h == "Dispatch_Id") else { continue };
        for rec in rdr.records().flatten() {
            if let Some(d) = rec.get(did_i).and_then(|s| s.trim().parse().ok()) {
                out.insert((proc.clone(), d));
            }
        }
    }
    out
}

/// Approximates rocprofv3's selection: the regex crate stands in for its regex engine, and the
/// dispatch-id order within each kernel symbol for its per-symbol iteration count.
fn approximate_selection(
    kernels: &BTreeMap<String, KernelDispatches>,
    include: Option<&Regex>,
    exclude: Option<&Regex>,
    iterations: &BTreeSet<u64>,
) -> BTreeSet<(String, u64)> {
    let mut out = BTreeSet::new();
    for (name, k) in kernels {
        if !include.is_none_or(|r| r.is_match(name)) || exclude.is_some_and(|r| r.is_match(name)) {
            continue;
        }
        for ((proc, _), ids) in &k.per_symbol {
            for (i, d) in ids.iter().enumerate() {
                if iterations.contains(&(i as u64 + 1)) {
                    out.insert((proc.clone(), *d));
                }
            }
        }
    }
    out
}

/// Port of rocprofv3's `get_kernel_filter_range`: `N` and inclusive `A-B` tokens, separated by
/// `[`, `]`, `,` or spaces.
fn parse_range(spec: &str) -> Result<BTreeSet<u64>, String> {
    let mut out = BTreeSet::new();
    for tok in spec.split(|c| "[], ".contains(c)).filter(|t| !t.is_empty()) {
        if let Some((a, b)) = tok.split_once('-') {
            let (Ok(a), Ok(b)) = (a.trim().parse::<u64>(), b.trim().parse::<u64>()) else {
                return Err(format!("bad range {tok:?}; expected A-B where A and B are numbers"));
            };
            if a > b {
                // rocprofv3 silently selects nothing from a reversed range.
                return Err(format!("range {tok:?} is reversed; write {b}-{a}"));
            }
            if b - a > 10_000_000 {
                return Err(format!("range {tok:?} is too long"));
            }
            out.extend(a..=b);
        } else {
            out.insert(tok.parse().map_err(|_| format!("expected an integer, got {tok:?}"))?);
        }
    }
    Ok(out)
}

/// The values of the last `--kernel-iteration-range`, joined the way rocprofv3 passes them on.
fn range_spec(opts: &[String]) -> Option<String> {
    let (toks, _) = split_options(opts, &[("--kernel-iteration-range", Arity::OneOrMore)]);
    let last = toks.iter().rposition(|t| t.starts_with("--kernel-iteration-range"))?;
    Some(match toks[last].split_once('=') {
        Some((_, v)) => v.to_string(),
        None => toks[last + 1..].join(", "),
    })
}

/// Splits rocprofv3 option tokens into the `wanted` options, with their values, and the rest.
/// Values follow rocprofv3's argparse: `FLAG=VALUE` carries one value inline; otherwise an
/// optional BOOL, and each value of a one-or-more option, is a following token not starting
/// with `-`.
fn split_options(opts: &[String], wanted: &[(&str, Arity)]) -> (Vec<String>, Vec<String>) {
    let (mut keep, mut rest) = (Vec::new(), Vec::new());
    let mut it = opts.iter().peekable();
    while let Some(t) = it.next() {
        let flag = t.split_once('=').map_or(t.as_str(), |(f, _)| f);
        let Some(&(_, arity)) = wanted.iter().find(|(n, _)| *n == flag) else {
            rest.push(t.clone());
            continue;
        };
        keep.push(t.clone());
        if t.contains('=') {
            continue;
        }
        match arity {
            Arity::One => keep.extend(it.next().cloned()),
            Arity::OptionalBool => keep.extend(it.next_if(|v| !v.starts_with('-')).cloned()),
            Arity::OneOrMore => {
                while let Some(v) = it.next_if(|v| !v.starts_with('-')) {
                    keep.push(v.clone());
                }
            }
        }
    }
    (keep, rest)
}

fn fail_options(errs: &Errors) -> ! {
    let out = json!({
        "error": {
            "options": errs.0.keys().collect::<Vec<_>>(),
            "messages": errs.0,
        }
    });
    print_json(&out);
    std::process::exit(2);
}

fn internal(msg: &str) -> ! {
    let out = json!({"error": {"options": [], "messages": {"internal": [msg]}}});
    print_json(&out);
    std::process::exit(1);
}

/// Prints one JSON object on stdout, leaving out null fields as the TOML files do.
fn print_json(v: &Value) {
    println!("{}", serde_json::to_string_pretty(&strip_nulls(v)).unwrap());
}

fn strip_nulls(v: &Value) -> Value {
    match v {
        Value::Object(m) => {
            Value::Object(m.iter().filter(|(_, x)| !x.is_null()).map(|(k, x)| (k.clone(), strip_nulls(x))).collect())
        }
        Value::Array(a) => Value::Array(a.iter().map(strip_nulls).collect()),
        _ => v.clone(),
    }
}

/// JSON to TOML: nulls are left out, and `started` becomes a TOML datetime.
fn json_to_toml(v: &Value, key: &str) -> Option<toml::Value> {
    Some(match v {
        Value::Null => return None,
        Value::Bool(b) => toml::Value::Boolean(*b),
        Value::Number(n) => match n.as_i64() {
            Some(i) => toml::Value::Integer(i),
            None => toml::Value::Float(n.as_f64().unwrap_or(0.0)),
        },
        Value::String(s) if key == "started" => match s.parse() {
            Ok(d) => toml::Value::Datetime(d),
            Err(_) => toml::Value::String(s.clone()),
        },
        Value::String(s) => toml::Value::String(s.clone()),
        Value::Array(a) => toml::Value::Array(a.iter().filter_map(|x| json_to_toml(x, "")).collect()),
        Value::Object(m) => {
            toml::Value::Table(m.iter().filter_map(|(k, x)| json_to_toml(x, k).map(|t| (k.clone(), t))).collect())
        }
    })
}

fn toml_to_json(v: &toml::Value) -> Value {
    match v {
        toml::Value::String(s) => json!(s),
        toml::Value::Integer(i) => json!(i),
        toml::Value::Float(f) => json!(f),
        toml::Value::Boolean(b) => json!(b),
        toml::Value::Datetime(d) => json!(d.to_string()),
        toml::Value::Array(a) => Value::Array(a.iter().map(toml_to_json).collect()),
        toml::Value::Table(t) => Value::Object(t.iter().map(|(k, x)| (k.clone(), toml_to_json(x))).collect::<Map<_, _>>()),
    }
}

fn write_meta(dir: &Path, meta: &Value) {
    let table = json_to_toml(meta, "").unwrap();
    let text = format!(
        "# Written by capture_att_trace.rs. See references/structured_output_directory_spec.md.\n{}",
        toml::to_string(&table).unwrap_or_else(|e| internal(&format!("serialize {META_FILE}: {e}")))
    );
    // Written to a temp file and renamed into place, so an interrupted write leaves the previous
    // version of the file intact.
    let tmp = dir.join(format!(".{META_FILE}.tmp-{}", std::process::id()));
    fs::write(&tmp, text)
        .and_then(|()| fs::rename(&tmp, dir.join(META_FILE)))
        .unwrap_or_else(|e| internal(&format!("write {META_FILE}: {e}")));
}

/// The current UTC time as (`YYYY-MM-DD-HH-MM-SS`, RFC 3339 with `Z`).
fn utc_now() -> (String, String) {
    let secs = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0);
    let (days, sod) = (secs.div_euclid(86400), secs.rem_euclid(86400));
    // Days since 1970-01-01 to a civil date (Howard Hinnant's algorithm).
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + i64::from(m <= 2);
    let (hh, mm, ss) = (sod / 3600, sod % 3600 / 60, sod % 60);
    (
        format!("{y:04}-{m:02}-{d:02}-{hh:02}-{mm:02}-{ss:02}"),
        format!("{y:04}-{m:02}-{d:02}T{hh:02}:{mm:02}:{ss:02}Z"),
    )
}

fn absolute(p: &Path) -> PathBuf {
    std::path::absolute(p).unwrap_or_else(|_| p.to_path_buf())
}

fn capture_output(cmd: &str, args: &[&str]) -> Option<String> {
    let out = Command::new(cmd).args(args).stdin(Stdio::null()).stderr(Stdio::null()).output().ok()?;
    out.status.success().then(|| String::from_utf8_lossy(&out.stdout).into_owned())
}

fn find_decoder(lib: &Path) -> Option<PathBuf> {
    let mut v: Vec<PathBuf> = fs::read_dir(lib)
        .ok()?
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.file_name().and_then(|n| n.to_str()).is_some_and(|n| n.starts_with("librocprof-trace-decoder.so")))
        .collect();
    v.sort();
    v.into_iter().next()
}


/// How one run of the command ended.
struct RunOutcome {
    /// The command's exit code, or 128 + the signal that ended it.
    rc: i32,
    /// The script killed the command's processes after a fatal error.
    killed: bool,
    /// The command or a process it started printed a glog fatal line (`F<mmdd> ...`).
    fatal: bool,
}

impl RunOutcome {
    /// The verdict and exit code of a failed run; `None` when it succeeded.
    fn failure(&self) -> Option<(&'static str, i32)> {
        if self.killed {
            Some((KILLED_VERDICT, 128 + libc::SIGKILL))
        } else if self.rc != 0 {
            Some(("FAILED: rocprofv3 failed", self.rc))
        } else if self.fatal {
            Some((FATAL_VERDICT, FATAL_EXIT))
        } else {
            None
        }
    }
}

/// A fatal line appeared, yet the command exited 0 with no process left to kill: for example, a
/// launcher that ignores its worker's failure. The exit code is that of the abort a fatal error
/// causes.
const FATAL_VERDICT: &str = "FAILED: rocprofv3 fatal error";
const FATAL_EXIT: i32 = 134;

/// How long to wait for the command's remaining output after it exits, or for its processes to die
/// after a kill.
const DRAIN_GRACE: Duration = Duration::from_secs(10);

/// Runs the command, copying its stdout and stderr to our stderr and to `log`. rocprofv3 execs the
/// application, so the child is the application itself. The script becomes the subreaper of the
/// processes the command starts, so a worker whose launcher exits stays in reach: once a glog fatal
/// line appears, the run lasts until all of them are gone, and those still running `FATAL_GRACE`
/// after the line are killed.
fn run_logged(full: &[String], log: &Path) -> RunOutcome {
    use std::os::unix::process::ExitStatusExt;
    // SAFETY: PR_SET_CHILD_SUBREAPER only sets a flag of this process.
    unsafe { libc::prctl(libc::PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) };
    let file = Arc::new(Mutex::new(fs::File::create(log).unwrap_or_else(|e| internal(&format!("create log: {e}")))));
    let note = |msg: String| {
        eprint!("{msg}");
        let _ = file.lock().unwrap().write_all(msg.as_bytes());
    };
    let mut child = match Command::new(&full[0]).args(&full[1..]).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn() {
        Ok(c) => c,
        Err(e) => {
            note(format!("cannot start {}: {e}\n", full[0]));
            return RunOutcome { rc: 127, killed: false, fatal: false };
        }
    };
    let fatal_at: Arc<Mutex<Option<Instant>>> = Arc::new(Mutex::new(None));
    let pump = |r: Box<dyn Read + Send>, f: Arc<Mutex<fs::File>>, fatal: Arc<Mutex<Option<Instant>>>| {
        thread::spawn(move || {
            let mut r = BufReader::new(r);
            let mut line = Vec::new();
            while r.read_until(b'\n', &mut line).map(|n| n > 0).unwrap_or(false) {
                let _ = std::io::stderr().write_all(&line);
                let _ = f.lock().unwrap().write_all(&line);
                if line.len() > 6 && line[0] == b'F' && line[1..5].iter().all(u8::is_ascii_digit) && line[5] == b' ' {
                    fatal.lock().unwrap().get_or_insert_with(Instant::now);
                }
                line.clear();
            }
        })
    };
    let t1 = pump(Box::new(child.stdout.take().unwrap()), file.clone(), fatal_at.clone());
    let t2 = pump(Box::new(child.stderr.take().unwrap()), file.clone(), fatal_at.clone());
    let me = std::process::id();
    let (mut status, mut exited_at, mut killed_at) = (None, None::<Instant>, None::<Instant>);
    let drained = loop {
        if status.is_none() {
            status = child.try_wait().unwrap_or_else(|e| internal(&format!("wait: {e}")));
            if status.is_some() {
                exited_at = Some(Instant::now());
            }
        }
        let drained = t1.is_finished() && t2.is_finished();
        let since_fatal = fatal_at.lock().unwrap().map(|t| t.elapsed());
        // After a fatal line, also wait for processes that closed their output: a hung worker can.
        if status.is_some() && drained && (since_fatal.is_none() || live_descendants(me).is_empty()) {
            break true;
        }
        if let (Some(elapsed), None) = (since_fatal, killed_at) {
            if elapsed >= FATAL_GRACE {
                let pids = live_descendants(me);
                note(format!(
                    "killing {} and the processes it started ({} in all), {}s after a fatal error\n",
                    full[0],
                    pids.len(),
                    FATAL_GRACE.as_secs()
                ));
                for pid in pids {
                    // SAFETY: kill only sends a signal.
                    unsafe { libc::kill(pid as libc::pid_t, libc::SIGKILL) };
                }
                killed_at = Some(Instant::now());
            }
        }
        // Without a fatal line, a process the command left behind can hold the output open
        // indefinitely; after a kill, a process stuck in the driver may not die.
        let waited = match (killed_at, exited_at) {
            (Some(t), _) => t.elapsed() >= DRAIN_GRACE,
            (None, Some(t)) => since_fatal.is_none() && t.elapsed() >= DRAIN_GRACE,
            (None, None) => false,
        };
        if waited {
            note(format!("output may be incomplete: not waiting any longer for the processes {} started\n", full[0]));
            break false;
        }
        thread::sleep(Duration::from_millis(200));
    };
    if drained {
        let _ = t1.join();
        let _ = t2.join();
    }
    let rc = match status {
        Some(s) => s.signal().map_or_else(|| s.code().unwrap_or(1), |sig| 128 + sig),
        None => 128 + libc::SIGKILL,
    };
    let fatal = fatal_at.lock().unwrap().is_some();
    RunOutcome { rc, killed: killed_at.is_some(), fatal }
}

/// The live (not zombie) processes descended from `root`, read from /proc.
fn live_descendants(root: u32) -> Vec<u32> {
    let mut children: BTreeMap<u32, Vec<(u32, bool)>> = BTreeMap::new();
    for e in fs::read_dir("/proc").into_iter().flatten().flatten() {
        let Some(pid) = e.file_name().to_str().and_then(|s| s.parse::<u32>().ok()) else { continue };
        let Ok(stat) = fs::read_to_string(e.path().join("stat")) else { continue };
        // `pid (comm) state ppid ...`; comm can hold spaces and parentheses.
        let Some((_, rest)) = stat.rsplit_once(')') else { continue };
        let mut fields = rest.split_whitespace();
        let (Some(state), Some(Ok(ppid))) = (fields.next(), fields.next().map(str::parse::<u32>)) else { continue };
        children.entry(ppid).or_default().push((pid, state != "Z"));
    }
    let (mut out, mut todo) = (Vec::new(), vec![root]);
    while let Some(p) = todo.pop() {
        for &(c, live) in children.get(&p).into_iter().flatten() {
            if live {
                out.push(c);
            }
            todo.push(c);
        }
    }
    out
}

fn walk(base: &Path, dir: &Path, ui: &mut Vec<String>, stats: &mut Vec<(String, u64)>, att: &mut u64) {
    let Ok(rd) = fs::read_dir(dir) else { return };
    for e in rd.flatten() {
        let p = e.path();
        let name = e.file_name().to_string_lossy().into_owned();
        let rel = p.strip_prefix(base).unwrap_or(&p).display().to_string();
        let Ok(ft) = e.file_type() else { continue };
        if ft.is_dir() {
            if name.starts_with("ui_output_agent_") && name.contains("_dispatch_") {
                ui.push(rel);
            } else {
                walk(base, &p, ui, stats, att);
            }
        } else if name.starts_with("stats_") && name.ends_with(".csv") {
            let lines = fs::read(&p).map(|b| b.split(|c| *c == b'\n').filter(|l| !l.is_empty()).count() as u64).unwrap_or(0);
            stats.push((rel, lines.saturating_sub(1)));
        } else if name.ends_with(".att") {
            *att += 1;
        }
    }
}

fn scan_warnings(log: &Path) -> Vec<Value> {
    let re = Regex::new(WARN_RE).unwrap();
    let text = fs::read(log).map(|b| String::from_utf8_lossy(&b).into_owned()).unwrap_or_default();
    let mut counts: BTreeMap<String, u64> = BTreeMap::new();
    for l in text.lines().filter(|l| re.is_match(l)) {
        *counts.entry(l.trim().to_string()).or_default() += 1;
    }
    let mut v: Vec<(String, u64)> = counts.into_iter().collect();
    v.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(&b.0)));
    v.into_iter().take(15).map(|(line, count)| json!({"count": count, "line": line})).collect()
}

fn shell_join(v: &[String]) -> String {
    v.iter()
        .map(|s| {
            if !s.is_empty() && s.chars().all(|c| c.is_ascii_alphanumeric() || "-_./=:,+@%".contains(c)) {
                s.clone()
            } else {
                format!("'{}'", s.replace('\'', r"'\''"))
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}

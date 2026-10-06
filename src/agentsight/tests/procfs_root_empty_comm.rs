//! `try_match_process` must not give up on a pid whose comm is readable but
//! empty. The `read_to_string` the scan path replaced read such a file back
//! as `Ok("")` and discovery still fell through to the cmdline rules; the
//! lossy `read_comm_from` (fb944ddb) collapses that case into "unreadable",
//! so the `?` dropped the pid before the rules ever ran. The kernel refuses
//! every in-place way to blank a live comm (`prctl(PR_SET_NAME, "")` is not
//! reachable from a test child and writing an empty/whitespace comm is
//! rejected), so this scenario replays the pid through a fake procfs tree
//! instead.
//!
//! Needs its own test binary because the procfs root is a process-global
//! (see `procfs_root.rs`): `set_proc_root` must win its `OnceLock` race
//! against no one here.
//!
//! Runs without eBPF; safe as a normal user.

use std::fs;

use agentsight::config::CmdlineRule;
use agentsight::discovery::scanner::AgentScanner;
use agentsight::utils::procfs::configure;

/// The probe pid inside the fake procfs tree.
const PROBE_PID: u32 = 4242;

/// A fake procfs whose probe looks exactly like the regression: a comm file
/// the kernel accepted but which carries no name, next to a cmdline that the
/// configured rules must still get to see.
fn fake_procfs_with_empty_comm() -> std::path::PathBuf {
    let dir =
        std::env::temp_dir().join(format!("agentsight-empty-comm-{}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    // A procfs root without a pid-1 entry looks like a misconfiguration to
    // configure(); give it one.
    std::fs::create_dir_all(dir.join("1")).expect("create fake procfs root");

    let probe = dir.join(PROBE_PID.to_string());
    fs::create_dir_all(&probe).expect("create probe dir");
    // Readable but name-less: the exact bytes a blanked comm leaves behind.
    fs::write(probe.join("comm"), b" \n").expect("write empty-ish comm");
    // The cmdline rules are the scan path's other strategy: they match.
    fs::write(probe.join("cmdline"), b"/usr/bin/agent-probe\0--serve\0")
        .expect("write cmdline");
    // try_match_process tolerates an unreadable exe, but provide one anyway.
    std::os::unix::fs::symlink("/usr/bin/agent-probe", probe.join("exe"))
        .expect("write exe symlink");

    dir
}

#[test]
fn try_match_process_still_matches_through_an_empty_comm() {
    let root = fake_procfs_with_empty_comm();
    configure(&root);

    let rules = vec![CmdlineRule {
        patterns: vec!["*agent-probe*".to_string()],
        agent_name: Some("AgentProbe".to_string()),
        allow: true,
    }];
    let scanner = AgentScanner::from_rules(&rules, &[]);

    let matched = scanner
        .try_match_process(PROBE_PID)
        .expect("an empty comm must not drop the pid before the cmdline rules run");
    assert_eq!(matched.agent_info.name, "AgentProbe");

    let _ = fs::remove_dir_all(&root);
}

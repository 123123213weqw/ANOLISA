// SPDX-License-Identifier: Apache-2.0
//! A consumer that stops reading early is a normal pipeline condition, not a
//! crash: `ktuner check | head -1` must not abort with a Rust panic and the
//! exit status of a failed process.
//!
//! The README's exit codes are the command's own verdict (0 = nothing to
//! recommend, 1 = recommendations, 2 = error). A closed pipe replaced them
//! with 101 — this crate sets no `panic = "abort"`, so the release profile
//! unwinds the same way the debug one CI runs does — plus a panic message a
//! pipeline consumer never asked for.
//! The sibling anolisa CLI was filed with exactly this symptom and fixed by
//! treating a closed stdout as a graceful stop (#1430); the CLI here still
//! panics.
//!
//! The one-page pipe is the capacity `pipe(7)` documents for a user at the
//! pipe-page soft limit ("if [it] is exceeded, newly created pipes have a
//! capacity of one page"), and it is what makes the condition deterministic:
//! `ktuner check`'s JSON cannot fit in one page, so the write that meets the
//! closed read end is guaranteed rather than a timing race.
#![cfg(target_os = "linux")]

use std::io::Read;
use std::os::fd::AsRawFd;
use std::os::unix::process::ExitStatusExt;
use std::process::{Command, Stdio};

/// The JSON `ktuner check` prints on this host; `None` when it has less than
/// one page to write (an error body goes to stderr, so this also covers the
/// error exit).
fn check_json_len() -> Option<usize> {
    let out = Command::new(env!("CARGO_BIN_EXE_ktuner"))
        .arg("check")
        .output()
        .expect("run ktuner check");
    (out.stdout.len() > 4096).then_some(out.stdout.len())
}

#[test]
fn a_closed_stdout_does_not_panic_the_cli() {
    // Precondition, not the assertion: the one-page pipe can only produce the
    // condition when the command has more than one page to write. A host with
    // nothing to recommend cannot exercise it (and does not need the fix).
    let Some(json_len) = check_json_len() else {
        eprintln!("skipping: `ktuner check` has no multi-page JSON report on this host");
        return;
    };

    let mut child = Command::new(env!("CARGO_BIN_EXE_ktuner"))
        .arg("check")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn ktuner check");
    let mut stdout = child.stdout.take().expect("piped stdout");

    // Shrink the pipe to one page (F_SETPIPE_SZ rounds up to the system page
    // size, so read back what the kernel granted and fail loudly if it is not
    // smaller than the report this host produces).
    let granted = unsafe { libc::fcntl(stdout.as_raw_fd(), libc::F_SETPIPE_SZ, 4096) };
    assert!(granted >= 4096, "F_SETPIPE_SZ failed");
    if (granted as usize) >= json_len {
        eprintln!(
            "skipping: the granted pipe ({} bytes) holds the whole report",
            granted
        );
        let _ = child.kill();
        let _ = child.wait();
        return;
    }
    let actual = unsafe { libc::fcntl(stdout.as_raw_fd(), libc::F_GETPIPE_SZ) };
    assert!(actual >= 4096, "F_GETPIPE_SZ failed");

    // The consumer reads a little and goes away.
    let mut head = [0u8; 16];
    let _ = stdout.read(&mut head);
    drop(stdout);

    let output = child.wait_with_output().expect("wait for ktuner check");
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        !stderr.contains("panicked"),
        "a closed stdout must not panic the CLI; stderr was:\n{stderr}"
    );
    assert_ne!(
        output.status.code(),
        Some(101),
        "the panic exit code replaced the command's own status; stderr was:\n{stderr}"
    );
    assert!(
        output.status.signal().is_none(),
        "the command was killed by a signal instead of finishing"
    );
}

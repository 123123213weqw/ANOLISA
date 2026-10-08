use super::{starts_native_intercept_candidate, NativeLineState};

#[test]
fn native_intercept_candidate_only_starts_at_line_start() {
    let mut state = NativeLineState::default();

    assert!(starts_native_intercept_candidate(b"/", &state, false));
    assert!(starts_native_intercept_candidate(
        b"?? hello",
        &state,
        false
    ));
    assert!(!starts_native_intercept_candidate(
        "你".as_bytes(),
        &state,
        false
    ));
    assert!(starts_native_intercept_candidate(
        "你".as_bytes(),
        &state,
        true
    ));
    state.observe_shell_bytes(b"vim .");
    assert!(!starts_native_intercept_candidate(b"/", &state, true));
    assert!(!starts_native_intercept_candidate(
        b"?? hello",
        &state,
        true
    ));
    assert!(!starts_native_intercept_candidate(
        "你".as_bytes(),
        &state,
        true
    ));

    state.observe_shell_bytes(b"\n");
    assert!(starts_native_intercept_candidate(b"/mode", &state, false));
}

#[test]
fn multiline_paste_flag_survives_same_chunk_submit() {
    // A paste tool or fast terminal can batch the multiline paste and the
    // submitting newline into one PTY read. The submit newline clears the
    // mirror, but the multiline-paste observation must survive until it is
    // taken: `observe_native_line` reads it right after this chunk, and
    // the #1932 F5 hint depends on the event firing.
    let mut state = NativeLineState::default();
    state.observe_shell_bytes(b"\x1b[200~cargo run\r\n--verbose\x1b[201~\r");
    assert!(
        state.take_multiline_paste_observed(),
        "a paste and its submitting newline in one read chunk must still report the multiline paste"
    );
}

#[test]
fn multiline_paste_flag_is_taken_exactly_once() {
    let mut state = NativeLineState::default();
    state.observe_shell_bytes(b"\x1b[200~a\nb\x1b[201~");
    assert!(state.take_multiline_paste_observed());
    assert!(!state.take_multiline_paste_observed());
}

//! Local PII detection, independent of transport, authorization, and storage.
//!
//! Verdicts classify findings; coverage records whether they describe the whole
//! input and configured detector set. Neither grants permission to an operation.

#![forbid(unsafe_code)]

mod builtin;
mod models;
mod python_unicode;
mod redact;
mod report;
mod scanner;
mod validators;

pub use models::{
    Coverage, CoverageStatus, PiiFinding, PiiScanOptions, PiiScanReport, PiiSummary, ScanError,
    ScanStatus, Severity, Source, Span, Verdict,
};
pub use scanner::PiiScanner;

/// Detection semantics version, independent of the `AgentSecCore` package version.
pub const SCANNER_VERSION: &str = "2.0.0";

//! Skill business contracts shared by clients, daemon applications and capabilities.

mod identity;

pub use identity::SkillIdentity;
use serde::{Deserialize, Serialize};

/// Invalid transport-independent Skill value or command selection.
#[derive(Debug, thiserror::Error)]
pub enum SkillSecInputError {
    /// A value violates the supported command or lexical identity contract.
    #[error("invalid SkillSec input: {0}")]
    Invalid(String),
}

/// Persisted user decision, distinct from a Hook's one-operation confirmation.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DecisionAction {
    /// Approve this version.
    Allow,
    /// Persist the existing always-allow behavior.
    AlwaysAllow,
    /// Hide this Skill.
    Block,
    /// Restore a selected trusted version.
    Rollback,
}

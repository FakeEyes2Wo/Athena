use serde::{Deserialize, Deserializer, Serialize, Serializer};
use std::convert::TryFrom;
use std::fmt;

// ── ValidationError ──

/// Error returned when a string fails non-blank validation.
#[derive(Debug, Clone)]
pub struct ValidationError(pub(crate) String);

impl fmt::Display for ValidationError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl std::error::Error for ValidationError {}

// ── NonBlankString ──

/// A string guaranteed to be non-empty and non-blank.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct NonBlankString(String);

impl NonBlankString {
    /// Create a new NonBlankString, validating that the input is non-blank.
    pub fn new(s: String) -> Result<Self, ValidationError> {
        if s.trim().is_empty() {
            Err(ValidationError("value cannot be empty or blank".into()))
        } else {
            Ok(NonBlankString(s))
        }
    }

    /// Return the inner string as a str.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl TryFrom<String> for NonBlankString {
    type Error = ValidationError;
    fn try_from(s: String) -> Result<Self, Self::Error> {
        NonBlankString::new(s)
    }
}

impl AsRef<str> for NonBlankString {
    fn as_ref(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for NonBlankString {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(f)
    }
}

impl Serialize for NonBlankString {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        self.0.serialize(serializer)
    }
}

impl<'de> Deserialize<'de> for NonBlankString {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let s = String::deserialize(deserializer)?;
        NonBlankString::try_from(s).map_err(serde::de::Error::custom)
    }
}

// ── Macro for newtype wrappers ──

macro_rules! id_newtype {
    ($name:ident, $doc:expr) => {
        #[doc = $doc]
        #[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
        #[serde(transparent)]
        pub struct $name(NonBlankString);

        impl $name {
            /// Create a new instance validating the input is non-blank.
            pub fn new(s: impl Into<String>) -> Result<Self, ValidationError> {
                NonBlankString::new(s.into()).map($name)
            }

            /// Return the inner string as a str.
            pub fn as_str(&self) -> &str {
                self.0.as_str()
            }
        }

        impl TryFrom<String> for $name {
            type Error = ValidationError;
            fn try_from(s: String) -> Result<Self, Self::Error> {
                NonBlankString::try_from(s).map($name)
            }
        }

        impl AsRef<str> for $name {
            fn as_ref(&self) -> &str {
                self.0.as_ref()
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                self.0.fmt(f)
            }
        }
    };
}

id_newtype!(ArtifactRef, "A stable reference to an external artifact.");
id_newtype!(CommitHash, "A git commit hash.");
id_newtype!(ThreadId, "A unique thread identifier.");
id_newtype!(TurnId, "A unique turn identifier.");
id_newtype!(SessionId, "A unique session identifier.");

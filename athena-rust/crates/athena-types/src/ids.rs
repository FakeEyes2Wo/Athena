use serde::{Deserialize, Deserializer, Serialize};
use std::fmt;

/// Error returned when an identifier is empty or blank.
#[derive(Debug, Clone)]
pub struct ValidationError(String);

impl fmt::Display for ValidationError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(f)
    }
}

impl std::error::Error for ValidationError {}

fn validate(value: String) -> Result<String, ValidationError> {
    if value.trim().is_empty() {
        Err(ValidationError("value cannot be empty or blank".into()))
    } else {
        Ok(value)
    }
}

macro_rules! id_newtype {
    ($name:ident, $doc:expr) => {
        #[doc = $doc]
        #[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize)]
        #[serde(transparent)]
        pub struct $name(String);

        impl $name {
            pub fn new(value: impl Into<String>) -> Result<Self, ValidationError> {
                validate(value.into()).map(Self)
            }

            pub fn as_str(&self) -> &str {
                &self.0
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                self.0.fmt(f)
            }
        }

        impl<'de> Deserialize<'de> for $name {
            fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
                Self::new(String::deserialize(deserializer)?).map_err(serde::de::Error::custom)
            }
        }
    };
}

id_newtype!(ArtifactRef, "A stable reference to an external artifact.");
id_newtype!(CommitHash, "A git commit hash.");
id_newtype!(ThreadId, "A unique thread identifier.");
id_newtype!(TurnId, "A unique turn identifier.");
id_newtype!(SessionId, "A unique session identifier.");

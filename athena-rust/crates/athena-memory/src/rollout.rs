use crate::context::ContextManager;
use crate::message::{MessagePart, MessageRole, ModelMessage};
use chrono::Datelike;
use std::path::{Path, PathBuf};
use tokio::fs::{File, OpenOptions};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader, BufWriter};

/// Records messages to a JSONL file for rollout/playback.
///
/// Each line is a structured JSON record — messages are **not** double-encoded.
///
/// Record types:
/// - **Message**: `{"seq":N, "ts":"...", "msg":<ModelMessage>}`
/// - **Compaction**: `{"seq":N, "ts":"...", "type":"compaction", "version":V, "summary":"..."}`
pub struct RolloutRecorder {
    project_root: PathBuf,
    path: Option<PathBuf>,
    fd: Option<BufWriter<File>>,
    seq: u64,
}

impl RolloutRecorder {
    pub fn new(project_root: PathBuf) -> Self {
        Self {
            project_root,
            path: None,
            fd: None,
            seq: 0,
        }
    }

    /// Return the current output file path, if open.
    pub fn path(&self) -> Option<&Path> {
        self.path.as_deref()
    }

    /// Open (or reuse) a JSONL file for the given `thread_id`.
    ///
    /// Creates the day directory under `<project_root>/.athena/sessions/...`
    /// and generates a unique file name. Idempotent — returns the already-open
    /// path on subsequent calls.
    pub async fn open(&mut self, thread_id: &str) -> Result<PathBuf, std::io::Error> {
        if let Some(ref p) = self.path {
            return Ok(p.clone());
        }

        let now = chrono::Utc::now();
        let day_dir = self
            .project_root
            .join(".athena")
            .join("sessions")
            .join(format!("{}", now.year()))
            .join(format!("{:02}", now.month()))
            .join(format!("{:02}", now.day()));
        tokio::fs::create_dir_all(&day_dir).await?;

        let short_id: String = thread_id
            .chars()
            .take(12)
            .map(|c| {
                if c.is_alphanumeric() || c == '-' || c == '_' {
                    c
                } else {
                    '_'
                }
            })
            .collect();
        let short_id = if short_id.is_empty() {
            "thread".to_string()
        } else {
            short_id
        };

        let file_name = format!(
            "rollout-{}-{}.jsonl",
            short_id,
            &uuid::Uuid::new_v4().to_string()[..8]
        );
        let full_path = day_dir.join(&file_name);
        self.path = Some(full_path.clone());

        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&full_path)
            .await?;
        self.fd = Some(BufWriter::new(file));
        self.seq = 0;
        Ok(full_path)
    }

    /// Write a structured message record.
    ///
    /// The message is embedded directly as a JSON object — it is **not**
    /// serialized to a string and then wrapped.
    pub async fn record(&mut self, msg: &ModelMessage) -> Result<(), std::io::Error> {
        let fd = match &mut self.fd {
            Some(f) => f,
            None => return Ok(()),
        };

        let line = serde_json::json!({
            "seq": self.seq,
            "ts": chrono::Utc::now().to_rfc3339(),
            "msg": msg,
        });
        let line_bytes = serde_json::to_vec(&line).map_err(std::io::Error::other)?;
        fd.write_all(&line_bytes).await?;
        fd.write_all(b"\n").await?;
        fd.flush().await?;
        self.seq += 1;
        Ok(())
    }

    /// Write a compaction checkpoint record.
    pub async fn record_compaction(
        &mut self,
        version: u64,
        summary: &str,
    ) -> Result<(), std::io::Error> {
        let fd = match &mut self.fd {
            Some(f) => f,
            None => return Ok(()),
        };

        let line = serde_json::json!({
            "seq": self.seq,
            "ts": chrono::Utc::now().to_rfc3339(),
            "type": "compaction",
            "version": version,
            "summary": summary,
        });
        let line_bytes = serde_json::to_vec(&line).map_err(std::io::Error::other)?;
        fd.write_all(&line_bytes).await?;
        fd.write_all(b"\n").await?;
        fd.flush().await?;
        self.seq += 1;
        Ok(())
    }

    /// Flush and close the output file. Idempotent.
    pub async fn close(&mut self) -> Result<(), std::io::Error> {
        if let Some(mut fd) = self.fd.take() {
            fd.flush().await?;
        }
        Ok(())
    }

    // ------------------------------------------------------------------
    // Recovery
    // ------------------------------------------------------------------

    /// Stream-read a JSONL rollout file and reconstruct a [`ContextManager`].
    ///
    /// * The latest compaction checkpoint resets the context (all earlier
    ///   messages are replaced by the summary).
    /// * Messages after the latest compaction are replayed on top.
    /// * A torn (incomplete) last record is silently skipped.
    pub async fn resume_context(
        path: &Path,
        context_limit: usize,
    ) -> Result<ContextManager, std::io::Error> {
        let file = File::open(path).await?;
        let reader = BufReader::new(file);
        let mut lines = reader.lines();

        let mut ctx = ContextManager::new(context_limit);

        while let Some(line) = lines.next_line().await? {
            let trimmed = line.trim().to_owned();
            if trimmed.is_empty() {
                continue;
            }

            let value: serde_json::Value = match serde_json::from_str(&trimmed) {
                Ok(v) => v,
                Err(_) => {
                    // Torn / corrupt record — skip silently.
                    continue;
                }
            };

            if Self::try_apply_compaction(&mut ctx, &value) {
                continue;
            }

            Self::try_apply_message(&mut ctx, &value);
        }

        Ok(ctx)
    }

    /// If `value` describes a compaction checkpoint, reset `ctx` to just
    /// the summary message and return `true`.
    fn try_apply_compaction(ctx: &mut ContextManager, value: &serde_json::Value) -> bool {
        let record_type = match value.get("type").and_then(|v| v.as_str()) {
            Some(t) => t,
            None => return false,
        };
        if record_type != "compaction" {
            return false;
        }
        let summary = value.get("summary").and_then(|v| v.as_str()).unwrap_or("");
        let n = ctx.items().len();
        let summary_msg = ModelMessage {
            role: MessageRole::System,
            parts: vec![MessagePart::SystemPrompt {
                content: format!("[HISTORY SUMMARY]\n{}", summary),
            }],
        };
        ctx.replace_range(0, n, vec![summary_msg]);
        true
    }

    /// If `value` carries a `msg` field, deserialize it and append to `ctx`.
    fn try_apply_message(ctx: &mut ContextManager, value: &serde_json::Value) {
        let msg_val = match value.get("msg") {
            Some(v) => v,
            None => return,
        };
        if let Ok(model_msg) = serde_json::from_value::<ModelMessage>(msg_val.clone()) {
            ctx.append(model_msg);
        }
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    fn user_msg(content: &str) -> ModelMessage {
        ModelMessage {
            role: MessageRole::User,
            parts: vec![MessagePart::UserPrompt {
                content: content.into(),
            }],
        }
    }

    fn test_dir() -> PathBuf {
        std::env::temp_dir().join(format!("rollout-test-{}", uuid::Uuid::new_v4()))
    }

    #[test]
    fn test_new() {
        let recorder = RolloutRecorder::new(PathBuf::from("/tmp"));
        assert!(recorder.path().is_none());
    }

    #[test]
    fn test_open_and_record() {
        let rt = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        rt.block_on(async {
            let dir = test_dir();
            let mut recorder = RolloutRecorder::new(dir.clone());

            let path = recorder.open("thread-abc").await.unwrap();
            assert!(path.exists());
            assert!(path.to_string_lossy().contains("rollout-thread-abc-"));

            let msg = user_msg("hello world");
            recorder.record(&msg).await.unwrap();

            // Read back and verify the msg field is an object, not a string.
            let content = tokio::fs::read_to_string(&path).await.unwrap();
            let line = content.trim();
            let parsed: serde_json::Value = serde_json::from_str(line).unwrap();
            assert_eq!(parsed["seq"], 0);
            assert!(parsed["ts"].is_string());
            // Msg should be an object with role + parts, NOT a string.
            assert!(
                parsed["msg"].is_object(),
                "msg should be a JSON object, not a string"
            );
            assert_eq!(parsed["msg"]["role"], "user");
            assert!(parsed["msg"]["parts"].is_array());

            recorder.close().await.unwrap();

            // Cleanup
            let _ = std::fs::remove_file(&path);
            let _ = std::fs::remove_dir_all(&dir);
        });
    }

    #[test]
    fn test_record_without_open() {
        let rt = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        rt.block_on(async {
            let mut recorder = RolloutRecorder::new(PathBuf::from("/tmp"));
            // Should be a no-op, not an error.
            recorder.record(&user_msg("nope")).await.unwrap();
        });
    }

    #[test]
    fn test_record_compaction_event() {
        let rt = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        rt.block_on(async {
            let dir = test_dir();
            let mut recorder = RolloutRecorder::new(dir.clone());
            recorder.open("test-thread").await.unwrap();

            recorder
                .record_compaction(42, "summarized 10 messages")
                .await
                .unwrap();

            let path = recorder.path().unwrap().to_path_buf();
            let content = tokio::fs::read_to_string(&path).await.unwrap();
            let line = content.trim();
            let parsed: serde_json::Value = serde_json::from_str(line).unwrap();
            assert_eq!(parsed["seq"], 0);
            assert_eq!(parsed["type"], "compaction");
            assert_eq!(parsed["version"], 42);
            assert_eq!(parsed["summary"], "summarized 10 messages");

            recorder.close().await.unwrap();

            let _ = std::fs::remove_file(&path);
            let _ = std::fs::remove_dir_all(&dir);
        });
    }

    #[test]
    fn test_close_idempotent() {
        let rt = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        rt.block_on(async {
            let dir = test_dir();
            let mut recorder = RolloutRecorder::new(dir.clone());
            recorder.open("test").await.unwrap();
            recorder.close().await.unwrap();
            // Second close should be safe.
            recorder.close().await.unwrap();
            let _ = std::fs::remove_dir_all(&dir);
        });
    }

    #[test]
    fn test_open_idempotent() {
        let rt = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        rt.block_on(async {
            let dir = test_dir();
            let mut recorder = RolloutRecorder::new(dir.clone());
            let path1 = recorder.open("test").await.unwrap();
            let path2 = recorder.open("test").await.unwrap();
            // Second open returns same path (reuses existing fd).
            assert_eq!(path1, path2);
            recorder.close().await.unwrap();
            let _ = std::fs::remove_dir_all(&dir);
        });
    }

    #[test]
    fn test_resume_context_basic() {
        let rt = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        rt.block_on(async {
            let dir = test_dir();
            let mut recorder = RolloutRecorder::new(dir.clone());
            let path = recorder.open("test").await.unwrap();

            recorder.record(&user_msg("first")).await.unwrap();
            recorder.record(&user_msg("second")).await.unwrap();

            recorder.close().await.unwrap();

            // Read back
            let ctx = RolloutRecorder::resume_context(&path, 10_000)
                .await
                .unwrap();
            assert_eq!(ctx.items().len(), 2);
            assert_eq!(
                ctx.items()[0].parts[0],
                MessagePart::UserPrompt {
                    content: "first".into()
                }
            );
            assert_eq!(
                ctx.items()[1].parts[0],
                MessagePart::UserPrompt {
                    content: "second".into()
                }
            );

            let _ = std::fs::remove_file(&path);
            let _ = std::fs::remove_dir_all(&dir);
        });
    }

    #[test]
    fn test_resume_context_with_compaction() {
        let rt = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        rt.block_on(async {
            let dir = test_dir();
            let mut recorder = RolloutRecorder::new(dir.clone());
            let path = recorder.open("test").await.unwrap();

            recorder.record(&user_msg("old message")).await.unwrap();
            recorder
                .record_compaction(2, "compacted old messages")
                .await
                .unwrap();
            recorder.record(&user_msg("after compact")).await.unwrap();

            recorder.close().await.unwrap();

            // Read back — only the message after the compaction survives
            // alongside the summary.
            let ctx = RolloutRecorder::resume_context(&path, 10_000)
                .await
                .unwrap();
            assert_eq!(ctx.items().len(), 2);
            assert_eq!(ctx.items()[0].role, MessageRole::System);
            assert!(match &ctx.items()[0].parts[0] {
                MessagePart::SystemPrompt { content } => content.contains("compacted old messages"),
                _ => false,
            });
            assert_eq!(
                ctx.items()[1].parts[0],
                MessagePart::UserPrompt {
                    content: "after compact".into()
                }
            );

            let _ = std::fs::remove_file(&path);
            let _ = std::fs::remove_dir_all(&dir);
        });
    }

    #[test]
    fn test_resume_context_torn_last_record() {
        let rt = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        rt.block_on(async {
            let dir = test_dir();
            let mut recorder = RolloutRecorder::new(dir.clone());
            let path = recorder.open("test").await.unwrap();

            recorder.record(&user_msg("good message")).await.unwrap();

            // Flush and then manually append a torn line
            recorder.close().await.unwrap();

            use std::io::Write;
            let mut f = std::fs::OpenOptions::new()
                .append(true)
                .open(&path)
                .unwrap();
            writeln!(f, "{{truncated incomplete line").unwrap();

            // Read back — torn line should be skipped, good message recovered.
            let ctx = RolloutRecorder::resume_context(&path, 10_000)
                .await
                .unwrap();
            assert_eq!(ctx.items().len(), 1);
            assert_eq!(
                ctx.items()[0].parts[0],
                MessagePart::UserPrompt {
                    content: "good message".into()
                }
            );

            let _ = std::fs::remove_file(&path);
            let _ = std::fs::remove_dir_all(&dir);
        });
    }

    #[test]
    fn test_sanitize_thread_id() {
        let rt = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        rt.block_on(async {
            let dir = test_dir();
            let mut recorder = RolloutRecorder::new(dir.clone());

            let path = recorder
                .open("dirty/../thread:name with spaces!")
                .await
                .unwrap();
            let filename = path.file_name().unwrap().to_string_lossy().to_string();

            // Unsafe chars should be replaced with '_'
            assert!(
                !filename.contains('/'),
                "path separators should be sanitized"
            );
            assert!(!filename.contains(':'), "colons should be sanitized");
            assert!(!filename.contains(' '), "spaces should be sanitized");
            assert!(!filename.contains('!'), "exclamation should be sanitized");
            // First 12 of "dirty/../thread:name with spaces!" = "dirty/../thr"
            // Sanitized: / → _, . → _, . → _, / → _ => "dirty____thr"
            assert!(
                filename.starts_with("rollout-dirty____thr"),
                "expected prefix 'rollout-dirty____thr' but filename was: {}",
                filename
            );

            recorder.close().await.unwrap();
            let _ = std::fs::remove_dir_all(&dir);
        });
    }
}

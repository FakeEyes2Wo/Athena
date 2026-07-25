use super::context::Message;
use chrono::Datelike;
use std::path::{Path, PathBuf};
use tokio::fs::{File, OpenOptions};
use tokio::io::{AsyncWriteExt, BufWriter};

/// Records messages to a JSONL file for rollout/playback.
///
/// Each line has the form:
/// ```json
/// {"seq": 0, "ts": "2026-07-25T12:00:00+00:00", "msg": "{\"role\":\"user\",\"content\":\"...\"}"}
/// ```
pub struct RolloutRecorder {
    project_root: PathBuf,
    path: Option<PathBuf>,
    fd: Option<BufWriter<File>>,
    seq: u64,
}

impl RolloutRecorder {
    pub fn new(project_root: PathBuf) -> Self {
        Self { project_root, path: None, fd: None, seq: 0 }
    }

    /// Return the current output file path, if open.
    pub fn path(&self) -> Option<&Path> {
        self.path.as_deref()
    }

    /// Open (or reuse) a JSONL file for the given `thread_id`.
    ///
    /// Creates the day directory under `<project_root>/.athena/sessions/...`
    /// and generates a unique file name.
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
            .map(|c| if c.is_alphanumeric() || c == '-' || c == '_' { c } else { '_' })
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

    /// Record a single message as a JSONL line.
    pub async fn record(&mut self, msg: &Message) -> Result<(), std::io::Error> {
        let fd = match &mut self.fd {
            Some(f) => f,
            None => return Ok(()),
        };

        let payload = serde_json::to_string(msg)
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))?;
        let line = serde_json::json!({
            "seq": self.seq,
            "ts": chrono::Utc::now().to_rfc3339(),
            "msg": payload,
        });
        let line_bytes = serde_json::to_string(&line)
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))?;
        fd.write_all(line_bytes.as_bytes()).await?;
        fd.write_all(b"\n").await?;
        fd.flush().await?;
        self.seq += 1;
        Ok(())
    }

    /// Record a compaction event.
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
            "type": "compaction",
            "version": version,
            "summary": summary,
        });
        let line_bytes = serde_json::to_string(&line)
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))?;
        fd.write_all(line_bytes.as_bytes()).await?;
        fd.write_all(b"\n").await?;
        fd.flush().await?;
        self.seq += 1;
        Ok(())
    }

    /// Flush and close the output file.
    pub async fn close(&mut self) -> Result<(), std::io::Error> {
        if let Some(mut fd) = self.fd.take() {
            fd.flush().await?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::context::Role;

    fn test_msg(content: &str) -> Message {
        Message {
            role: Role::User,
            content: content.into(),
            tool_calls: vec![],
            tool_call_id: None,
            tool_name: None,
        }
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
            let dir = std::env::temp_dir().join(format!(
                "rollout-test-{}",
                uuid::Uuid::new_v4()
            ));
            let mut recorder = RolloutRecorder::new(dir.clone());

            let path = recorder.open("thread-abc").await.unwrap();
            assert!(path.exists());
            assert!(path.to_string_lossy().contains("rollout-thread-abc-"));

            let msg = test_msg("hello world");
            recorder.record(&msg).await.unwrap();

            // Read back and verify format
            let content = tokio::fs::read_to_string(&path).await.unwrap();
            let line = content.trim();
            let parsed: serde_json::Value = serde_json::from_str(line).unwrap();
            assert_eq!(parsed["seq"], 0);
            assert!(parsed["ts"].is_string());
            assert!(parsed["msg"].is_string());

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
            // Should be a no-op, not an error
            recorder.record(&test_msg("nope")).await.unwrap();
        });
    }

    #[test]
    fn test_record_compaction_event() {
        let rt = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        rt.block_on(async {
            let dir = std::env::temp_dir().join(format!(
                "rollout-test-{}",
                uuid::Uuid::new_v4()
            ));
            let mut recorder = RolloutRecorder::new(dir.clone());
            recorder.open("test-thread").await.unwrap();

            recorder
                .record_compaction(42, "summarized 10 messages")
                .await
                .unwrap();

            // Read back to verify format
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
            let dir = std::env::temp_dir().join(format!(
                "rollout-test-{}",
                uuid::Uuid::new_v4()
            ));
            let mut recorder = RolloutRecorder::new(dir.clone());
            recorder.open("test").await.unwrap();
            recorder.close().await.unwrap();
            // Second close should be safe
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
            let dir = std::env::temp_dir().join(format!(
                "rollout-test-{}",
                uuid::Uuid::new_v4()
            ));
            let mut recorder = RolloutRecorder::new(dir.clone());
            let path1 = recorder.open("test").await.unwrap();
            let path2 = recorder.open("test").await.unwrap();
            // Second open returns same path (reuses existing fd)
            assert_eq!(path1, path2);
            recorder.close().await.unwrap();
            let _ = std::fs::remove_dir_all(&dir);
        });
    }
}

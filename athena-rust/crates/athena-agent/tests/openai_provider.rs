use athena_agent::{AgentConfig, LlmProvider, OpenAiProvider, ProviderEvent};
use futures::StreamExt;
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{oneshot, watch};

async fn read_request(stream: &mut TcpStream) -> std::io::Result<()> {
    let mut request = Vec::new();
    let mut buffer = [0; 1024];
    let header_end = loop {
        let count = stream.read(&mut buffer).await?;
        if count == 0 {
            return Ok(());
        }
        request.extend_from_slice(&buffer[..count]);
        if let Some(pos) = request.windows(4).position(|window| window == b"\r\n\r\n") {
            break pos + 4;
        }
    };
    let headers = String::from_utf8_lossy(&request[..header_end]);
    let body_len = headers
        .lines()
        .find_map(|line| {
            let (name, value) = line.split_once(':')?;
            name.eq_ignore_ascii_case("content-length")
                .then(|| value.trim().parse::<usize>().ok())
                .flatten()
        })
        .unwrap_or(0);
    while request.len() < header_end + body_len {
        let count = stream.read(&mut buffer).await?;
        if count == 0 {
            break;
        }
        request.extend_from_slice(&buffer[..count]);
    }
    Ok(())
}

async fn server_with_chunks(
    status: &str,
    chunks: Vec<Vec<u8>>,
) -> (String, tokio::task::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let status = status.to_string();
    let task = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        read_request(&mut socket).await.unwrap();
        socket
            .write_all(
                format!(
                    "HTTP/1.1 {status}\r\nTransfer-Encoding: chunked\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n"
                )
                .as_bytes(),
            )
            .await
            .unwrap();
        for chunk in chunks {
            socket
                .write_all(format!("{:X}\r\n", chunk.len()).as_bytes())
                .await
                .unwrap();
            socket.write_all(&chunk).await.unwrap();
            socket.write_all(b"\r\n").await.unwrap();
            socket.flush().await.unwrap();
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        socket.write_all(b"0\r\n\r\n").await.unwrap();
    });
    (format!("http://{address}"), task)
}

async fn events_from(url: String) -> Vec<ProviderEvent> {
    let provider = OpenAiProvider::new(url, "test-key");
    let config = AgentConfig::new("test-model", "system");
    let (_cancel_tx, cancel_rx) = watch::channel(false);
    let mut stream = provider.stream(&config, vec![], vec![], cancel_rx).await;
    let mut events = Vec::new();
    while let Some(event) = stream.next().await {
        let completed = matches!(event, ProviderEvent::ResponseCompleted { .. });
        events.push(event);
        if completed {
            break;
        }
    }
    events
}

#[tokio::test]
async fn non_success_http_status_is_provider_error() {
    let (url, server) = server_with_chunks("503 Service Unavailable", vec![]).await;
    let events = events_from(url).await;
    server.await.unwrap();
    assert!(
        matches!(events.as_slice(), [ProviderEvent::Error { message }] if message.contains("503"))
    );
}

#[tokio::test]
async fn stream_eof_without_done_marker_is_not_success() {
    let body = br#"data: {"choices":[{"delta":{"content":"partial"},"finish_reason":"stop"}]}
"#
    .to_vec();
    let (url, server) = server_with_chunks("200 OK", vec![body]).await;
    let events = events_from(url).await;
    server.await.unwrap();
    assert!(events.iter().any(
        |event| matches!(event, ProviderEvent::Error { message } if message.contains("[DONE]"))
    ));
    assert!(
        !events
            .iter()
            .any(|event| matches!(event, ProviderEvent::ResponseCompleted { .. }))
    );
}

#[tokio::test]
async fn utf8_character_split_between_http_chunks_is_preserved() {
    let mut body = br#"data: {"choices":[{"delta":{"content":""#.to_vec();
    body.extend_from_slice("你".as_bytes());
    body.extend_from_slice(
        br#""}}]}

data: [DONE]

"#,
    );
    let split = body
        .windows(3)
        .position(|bytes| bytes == "你".as_bytes())
        .unwrap()
        + 1;
    let (url, server) = server_with_chunks(
        "200 OK",
        vec![body[..split].to_vec(), body[split..].to_vec()],
    )
    .await;
    let events = events_from(url).await;
    server.await.unwrap();
    assert!(
        events
            .iter()
            .any(|event| matches!(event, ProviderEvent::TextDelta { delta, .. } if delta == "你"))
    );
    assert!(
        events
            .iter()
            .any(|event| matches!(event, ProviderEvent::ResponseCompleted { .. }))
    );
}

#[tokio::test]
async fn cancellation_while_waiting_for_response_headers_terminates_stream() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let (accepted_tx, accepted_rx) = oneshot::channel();
    let (release_tx, release_rx) = oneshot::channel();
    let server = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        read_request(&mut socket).await.unwrap();
        let _ = accepted_tx.send(());
        let _ = release_rx.await;
    });

    let provider = OpenAiProvider::new(format!("http://{address}"), "test-key");
    let config = AgentConfig::new("test-model", "system");
    let (cancel_tx, cancel_rx) = watch::channel(false);
    let mut stream = provider.stream(&config, vec![], vec![], cancel_rx).await;
    accepted_rx.await.unwrap();
    cancel_tx.send(true).unwrap();
    let event = tokio::time::timeout(Duration::from_millis(500), stream.next())
        .await
        .unwrap()
        .unwrap();
    assert!(matches!(event, ProviderEvent::Error { message } if message == "cancelled"));
    let _ = release_tx.send(());
    server.await.unwrap();
}

#[tokio::test]
async fn cancellation_while_waiting_for_next_chunk_terminates_stream() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let (chunk_sent_tx, chunk_sent_rx) = oneshot::channel();
    let (release_tx, release_rx) = oneshot::channel();
    let server = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        read_request(&mut socket).await.unwrap();
        socket
            .write_all(
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n",
            )
            .await
            .unwrap();
        let chunk = b"data: {\"choices\":[";
        socket
            .write_all(format!("{:X}\r\n", chunk.len()).as_bytes())
            .await
            .unwrap();
        socket.write_all(chunk).await.unwrap();
        socket.write_all(b"\r\n").await.unwrap();
        socket.flush().await.unwrap();
        let _ = chunk_sent_tx.send(());
        let _ = release_rx.await;
    });

    let provider = OpenAiProvider::new(format!("http://{address}"), "test-key");
    let config = AgentConfig::new("test-model", "system");
    let (cancel_tx, cancel_rx) = watch::channel(false);
    let mut stream = provider.stream(&config, vec![], vec![], cancel_rx).await;
    chunk_sent_rx.await.unwrap();
    cancel_tx.send(true).unwrap();
    let event = tokio::time::timeout(Duration::from_millis(500), stream.next())
        .await
        .unwrap()
        .unwrap();
    assert!(matches!(event, ProviderEvent::Error { message } if message == "cancelled"));
    let _ = release_tx.send(());
    server.await.unwrap();
}

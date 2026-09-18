//! List S3 object versions for a single doc.

use anyhow::{Context, Result};
use std::sync::Arc;
use y_sweet_core::store::Store;

pub async fn run(
    store: Arc<Box<dyn Store>>,
    relay_id: &str,
    doc_guid: &str,
    limit: Option<usize>,
) -> Result<()> {
    store.init().await.context("store init failed")?;

    let key = format!("{}-{}/data.ysweet", relay_id, doc_guid);
    let mut versions = store
        .list_versions(&key)
        .await
        .with_context(|| format!("list_versions failed for {}", key))?;

    // Newest first.
    versions.sort_by(|a, b| b.last_modified.cmp(&a.last_modified));

    let total = versions.len();
    let shown: Vec<_> = if let Some(n) = limit {
        versions.into_iter().take(n).collect()
    } else {
        versions
    };

    println!("doc        = {}", doc_guid);
    println!("storage key = {}", key);
    println!("total versions = {}{}", total, {
        if shown.len() < total {
            format!(" (showing {})", shown.len())
        } else {
            String::new()
        }
    });
    println!();
    println!("{:<40}  {:<30}  {}", "VERSION ID", "MODIFIED", "LATEST");
    for v in shown {
        let modified = format_ts(v.last_modified);
        let latest = if v.is_latest { "*" } else { "" };
        println!("{:<40}  {:<30}  {}", v.version_id, modified, latest);
    }

    Ok(())
}

fn format_ts(ms: u64) -> String {
    use chrono::{DateTime, Utc};
    let secs = (ms / 1000) as i64;
    let sub_ms = (ms % 1000) as u32;
    DateTime::<Utc>::from_timestamp(secs, sub_ms * 1_000_000)
        .map(|dt| dt.format("%Y-%m-%d %H:%M:%S UTC").to_string())
        .unwrap_or_else(|| format!("{}", ms))
}

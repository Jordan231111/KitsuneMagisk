use pb_rs::ConfigBuilder;
use pb_rs::types::FileDescriptor;

use crate::codegen::gen_cxx_binding;

#[path = "../include/codegen.rs"]
mod codegen;

#[allow(clippy::unwrap_used)]
fn main() {
    println!("cargo:rerun-if-changed=resetprop/proto/persistent_properties.proto");

    gen_cxx_binding("core-rs");

    // Cargo runs this script concurrently for each target ABI. pb-rs appends
    // to mod.rs in several writes, so a shared source directory races readers.
    let output = std::env::var("OUT_DIR").expect("Cargo did not provide OUT_DIR");
    // We include the message directly, not pb-rs's module index. Discard a
    // possibly partial index left by an interrupted invocation.
    let index = std::path::Path::new(&output).join("mod.rs");
    if index.exists() {
        std::fs::remove_file(index).expect("Cannot remove incomplete protobuf module index");
    }
    let output = output.as_str();
    let cb = ConfigBuilder::new(
        &["resetprop/proto/persistent_properties.proto"],
        None,
        Some(&output),
        &["."],
    )
    .unwrap();
    FileDescriptor::run(
        &cb.headers(false)
            .single_module(true)
            .dont_use_cow(true)
            .generate_getters(true)
            .build(),
    )
    .unwrap();
}

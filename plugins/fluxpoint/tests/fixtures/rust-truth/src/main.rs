//! Two independent readings of a Rust file: tokens by proc-macro2, item
//! extents by syn. See README.md beside the manifest.
use proc_macro2::{Delimiter, TokenStream, TokenTree};
use std::{collections::BTreeMap, env, fs, str::FromStr};
use syn::{
    punctuated::Punctuated,
    spanned::Spanned,
    visit::{self, Visit},
    AttrStyle, Attribute, Expr, Lit, Meta, Token,
};

const FLAGS: [&str; 4] = ["test", "kani", "fuzzing", "doctest"];
const FEATURES: [&str; 2] = ["runtime-benchmarks", "try-runtime"];
const TESTS: [&str; 8] = [
    "test",
    "rstest",
    "test_case",
    "test_matrix",
    "bench",
    "quickcheck",
    "proptest",
    "wasm_bindgen_test",
];

/// A script's interpreter line is not Rust; both readers drop it.
fn source(path: &str) -> Option<String> {
    let text = fs::read_to_string(path).ok()?;
    if text.starts_with("#!") && !text[2..].trim_start().starts_with('[') {
        return Some(
            text.split_once('\n')
                .map(|(_, rest)| rest.to_string())
                .unwrap_or_default(),
        );
    }
    Some(text)
}

// ---------------------------------------------------------------- tokens

fn flatten(stream: TokenStream, out: &mut Vec<String>) {
    for tree in stream {
        match tree {
            TokenTree::Group(group) => {
                let (open, close) = match group.delimiter() {
                    Delimiter::Parenthesis => ("(", ")"),
                    Delimiter::Brace => ("{", "}"),
                    Delimiter::Bracket => ("[", "]"),
                    Delimiter::None => ("", ""),
                };
                out.push(open.to_string());
                flatten(group.stream(), out);
                out.push(close.to_string());
            }
            TokenTree::Ident(ident) => out.push(format!("I:{ident}")),
            TokenTree::Punct(punct) => out.push(punct.as_char().to_string()),
            TokenTree::Literal(_) => out.push("L:".to_string()),
        }
    }
}

fn tokens(path: &str) {
    let Some(text) = source(path) else { return };
    let Ok(stream) = TokenStream::from_str(&text) else {
        println!("LEXFAIL {path}");
        return;
    };
    let mut flat = Vec::new();
    flatten(stream, &mut flat);
    let at = |index: usize| flat.get(index).map(String::as_str).unwrap_or("");
    let mut counts: BTreeMap<&str, usize> = BTreeMap::new();
    for index in 0..flat.len() {
        let before = if index > 0 { at(index - 1) } else { "" };
        let macro_call = at(index + 1) == "!" && matches!(at(index + 2), "(" | "[" | "{");
        let name = match at(index) {
            "I:unsafe" => "unsafe",
            "I:unwrap" | "I:unwrap_err"
                if before == "." && at(index + 1) == "(" && at(index + 2) == ")" =>
            {
                "unwrap"
            }
            "I:expect" | "I:expect_err" if before == "." && at(index + 1) == "(" => "expect_any",
            "I:panic" | "I:unreachable" if macro_call => "panic_macro",
            "I:todo" | "I:unimplemented" if macro_call => "todo",
            "I:let" if at(index + 1) == "I:_" && at(index + 2) == "=" => "let_underscore_raw",
            "I:mod" if at(index + 1).starts_with("I:") && at(index + 2) == ";" => "mod_decl",
            "#" if at(index + 1) == "[" && at(index + 2) == "I:allow" && at(index + 3) == "(" => {
                "allow_attr"
            }
            "#" if at(index + 1) == "!" && at(index + 2) == "[" && at(index + 3) == "I:allow" => {
                "allow_attr"
            }
            _ => continue,
        };
        *counts.entry(name).or_default() += 1;
    }
    let line: Vec<String> = counts
        .iter()
        .map(|(name, count)| format!("{name}={count}"))
        .collect();
    println!("FILE {path} {}", line.join(" "));
}

// --------------------------------------------------------------- extents

fn requires_nonprod(meta: &Meta) -> bool {
    match meta {
        Meta::Path(path) => path
            .get_ident()
            .is_some_and(|ident| FLAGS.contains(&ident.to_string().as_str())),
        Meta::NameValue(pair) => {
            let Expr::Lit(value) = &pair.value else {
                return false;
            };
            let Lit::Str(text) = &value.lit else {
                return false;
            };
            pair.path.is_ident("feature") && FEATURES.contains(&text.value().as_str())
        }
        Meta::List(list) => {
            let Ok(parts) = list.parse_args_with(Punctuated::<Meta, Token![,]>::parse_terminated)
            else {
                return false;
            };
            if list.path.is_ident("all") {
                parts.iter().any(requires_nonprod)
            } else if list.path.is_ident("any") {
                !parts.is_empty() && parts.iter().all(requires_nonprod)
            } else {
                false
            }
        }
    }
}

fn test_only(attributes: &[Attribute]) -> bool {
    attributes.iter().any(|attribute| {
        if !matches!(attribute.style, AttrStyle::Outer) {
            return false;
        }
        let path: Vec<String> = attribute
            .path()
            .segments
            .iter()
            .map(|part| part.ident.to_string())
            .collect();
        if path == ["cfg"] {
            return attribute
                .parse_args::<Meta>()
                .is_ok_and(|meta| requires_nonprod(&meta));
        }
        path.last()
            .is_some_and(|last| TESTS.contains(&last.as_str()))
            || (path.len() == 2 && path[0] == "kani" && path[1].starts_with("proof"))
    })
}

#[derive(Default)]
struct Extents {
    ends: Vec<String>,
}

impl Extents {
    fn note<T: Spanned>(&mut self, attributes: &[Attribute], node: &T, kind: &str) {
        if test_only(attributes) {
            let end = node.span().end();
            self.ends
                .push(format!("{}:{}:{kind}", end.line, end.column));
        }
    }
}

fn item_attributes(item: &syn::Item) -> &[Attribute] {
    use syn::Item::*;
    match item {
        Const(x) => &x.attrs,
        Enum(x) => &x.attrs,
        ExternCrate(x) => &x.attrs,
        Fn(x) => &x.attrs,
        ForeignMod(x) => &x.attrs,
        Impl(x) => &x.attrs,
        Macro(x) => &x.attrs,
        Mod(x) => &x.attrs,
        Static(x) => &x.attrs,
        Struct(x) => &x.attrs,
        Trait(x) => &x.attrs,
        TraitAlias(x) => &x.attrs,
        Type(x) => &x.attrs,
        Union(x) => &x.attrs,
        Use(x) => &x.attrs,
        _ => &[],
    }
}

impl<'ast> Visit<'ast> for Extents {
    fn visit_item(&mut self, item: &'ast syn::Item) {
        self.note(item_attributes(item), item, "item");
        visit::visit_item(self, item);
    }
    fn visit_impl_item(&mut self, item: &'ast syn::ImplItem) {
        use syn::ImplItem::*;
        let attributes: &[Attribute] = match item {
            Const(x) => &x.attrs,
            Fn(x) => &x.attrs,
            Type(x) => &x.attrs,
            Macro(x) => &x.attrs,
            _ => &[],
        };
        self.note(attributes, item, "item");
        visit::visit_impl_item(self, item);
    }
    fn visit_trait_item(&mut self, item: &'ast syn::TraitItem) {
        use syn::TraitItem::*;
        let attributes: &[Attribute] = match item {
            Const(x) => &x.attrs,
            Fn(x) => &x.attrs,
            Type(x) => &x.attrs,
            Macro(x) => &x.attrs,
            _ => &[],
        };
        self.note(attributes, item, "item");
        visit::visit_trait_item(self, item);
    }
    fn visit_foreign_item(&mut self, item: &'ast syn::ForeignItem) {
        use syn::ForeignItem::*;
        let attributes: &[Attribute] = match item {
            Fn(x) => &x.attrs,
            Static(x) => &x.attrs,
            Type(x) => &x.attrs,
            Macro(x) => &x.attrs,
            _ => &[],
        };
        self.note(attributes, item, "item");
        visit::visit_foreign_item(self, item);
    }
    fn visit_local(&mut self, local: &'ast syn::Local) {
        self.note(&local.attrs, local, "item");
        visit::visit_local(self, local);
    }
    // A field, a variant and an arm end at their own last token; whether the
    // comma after them belongs to them is a matter of taste, so these are
    // compared to within one column.
    fn visit_field(&mut self, field: &'ast syn::Field) {
        self.note(&field.attrs, field, "other");
        visit::visit_field(self, field);
    }
    fn visit_variant(&mut self, variant: &'ast syn::Variant) {
        self.note(&variant.attrs, variant, "other");
        visit::visit_variant(self, variant);
    }
    fn visit_arm(&mut self, arm: &'ast syn::Arm) {
        self.note(&arm.attrs, arm, "other");
        visit::visit_arm(self, arm);
    }
    // A parameter ends where its list does, and two of the three lists
    // close with a token that pairs with nothing: `>` and `|`.
    fn visit_generic_param(&mut self, param: &'ast syn::GenericParam) {
        use syn::GenericParam::*;
        let attributes: &[Attribute] = match param {
            Lifetime(x) => &x.attrs,
            Type(x) => &x.attrs,
            Const(x) => &x.attrs,
        };
        self.note(attributes, param, "other");
        visit::visit_generic_param(self, param);
    }
    fn visit_fn_arg(&mut self, argument: &'ast syn::FnArg) {
        let attributes: &[Attribute] = match argument {
            syn::FnArg::Receiver(x) => &x.attrs,
            syn::FnArg::Typed(x) => &x.attrs,
        };
        self.note(attributes, argument, "other");
        visit::visit_fn_arg(self, argument);
    }
    fn visit_expr_closure(&mut self, closure: &'ast syn::ExprClosure) {
        for input in &closure.inputs {
            let attributes: &[Attribute] = match input {
                syn::Pat::Ident(x) => &x.attrs,
                syn::Pat::Type(x) => &x.attrs,
                syn::Pat::Wild(x) => &x.attrs,
                syn::Pat::Reference(x) => &x.attrs,
                syn::Pat::Tuple(x) => &x.attrs,
                _ => &[],
            };
            self.note(attributes, input, "other");
        }
        visit::visit_expr_closure(self, closure);
    }
    // A block written as a statement ends at its brace, `const { .. }`
    // included, which opens with the word that also opens an item.
    fn visit_expr_block(&mut self, block: &'ast syn::ExprBlock) {
        self.note(&block.attrs, block, "item");
        visit::visit_expr_block(self, block);
    }
    fn visit_expr_unsafe(&mut self, block: &'ast syn::ExprUnsafe) {
        self.note(&block.attrs, block, "item");
        visit::visit_expr_unsafe(self, block);
    }
    fn visit_expr_const(&mut self, block: &'ast syn::ExprConst) {
        self.note(&block.attrs, block, "item");
        visit::visit_expr_const(self, block);
    }
}

fn extents(path: &str) {
    let Some(text) = source(path) else { return };
    let Ok(file) = syn::parse_file(&text) else {
        println!("PARSEFAIL {path}");
        return;
    };
    let mut found = Extents::default();
    found.visit_file(&file);
    let whole_file = file.attrs.iter().any(|attribute| {
        matches!(attribute.style, AttrStyle::Inner(_))
            && attribute.path().is_ident("cfg")
            && attribute
                .parse_args::<Meta>()
                .is_ok_and(|meta| requires_nonprod(&meta))
    });
    println!(
        "FILE {path} inner={} {}",
        u8::from(whole_file),
        found.ends.join(" ")
    );
}

fn main() {
    let mut arguments = env::args().skip(1);
    let mode = arguments.next().unwrap_or_default();
    for path in arguments {
        match mode.as_str() {
            "tokens" => tokens(&path),
            "extents" => extents(&path),
            _ => {
                eprintln!("usage: rust-truth tokens|extents FILE...");
                std::process::exit(2);
            }
        }
    }
}

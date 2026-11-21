mod common;

use crate::common::setup;
use fixture::Foo;

#[test]
fn integration() {
    setup();
    assert_eq!(fixture::make(1).size, 1);
}

fn good() -> u8 {
    1
}

fn bad( {
    good()
}

fn after() -> u8 {
    good()
}

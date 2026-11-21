mod cli;

use crate::cli::run;
use fixture::make;

fn main() {
    run(make(1));
}

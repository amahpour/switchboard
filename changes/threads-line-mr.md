### Fixed

- **A review board on a GitLab merge request says "MR".** The reported threads line read "2 open on the PR" on every board. On a GitLab merge request it now reads "2 open on the MR", in `show` and in the board's header, and `show` labels it "The merge request:". GitHub boards still say PR.

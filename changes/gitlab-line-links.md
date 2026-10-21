### Fixed

- **On a GitLab merge request, a finding's Lines link opens its lines.** The link used to open the file in the merge request's diff, at the top, though its label said it was the lines. It now opens the file at the board's head commit with the lines highlighted ("lines 40–58 at abc1234"), and a second link, "file in the MR diff", opens the colored diff, labelled for what it does. A board opened with no head has only the diff link. GitHub's links are unchanged.

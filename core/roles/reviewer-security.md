# Lens: security

Whether this change opens a hole.

- **Access control**: every new or changed endpoint, handler or query checks who
  the caller is and that they own what they touch. Database or storage rules
  that open data to the wrong readers.
- **Input**: user input is validated, and never reaches SQL, shell commands,
  file paths or templates unescaped.
- **Output**: raw HTML rendering, markdown rendering, and user-supplied strings
  put into pages, URLs or meta tags.
- **Data exposure**: responses, logs or public documents that leak ids, emails,
  tokens or internal errors.
- **Payments and identity**: anything that can grant access, skip a signature
  check, or change who a user is.
- **Abuse**: public endpoints that can be spammed without a rate limit.
- **Secrets**: keys, credentials or environment values in commits.
- **Dependencies**: new packages, and whether they are needed and maintained.

For each finding, give its impact: who could do what. CHANGES is for real,
blocking problems in this branch's change. Don't block on issues that were
already there and that the change didn't make worse; mention them under Notes.

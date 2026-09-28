// A fresh navigation avoids applying the local form's redirect restrictions to
// Samsung. The server constructs this link after validating login and CSRF.
const authorizationLink = document.getElementById("smartthings-authorize");
if (authorizationLink) window.location.replace(authorizationLink.href);

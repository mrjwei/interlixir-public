// CloudFront Function (cloudfront-js-2.0), viewer request, attached to the DEFAULT behaviour only (web bucket).
// Deep links such as /projects/abc/jobs/xyz have no object in S3; serve index.html for them so the SPA's
// router takes over. Paths with a file extension (assets, favicon) pass through untouched. /a3/* and
// /v1/auth/* are separate behaviours, so their real 401/404 responses are never rewritten.
function handler(event) {
  var request = event.request
  var last = request.uri.split('/').pop()
  if (last.indexOf('.') === -1) request.uri = '/index.html'
  return request
}

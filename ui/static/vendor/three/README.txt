Three.js 0.186.1 (MIT), from https://registry.npmjs.org/three/-/three-0.186.1.tgz (integrity checked).
build/three.module(.min).js, build/three.core(.min).js, and examples/jsm postprocessing + shaders used by the orb.
The add-ons import "three" by bare name; here that is rewritten to a relative path, because the page's
Content-Security-Policy allows no inline import map.

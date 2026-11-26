// Render the same world with a 2D context on browsers that cannot start WebGL.
export function createSoftwareRenderer(Three) {
  const canvas = document.createElement('canvas');
  const context = canvas.getContext('2d', { alpha: true });
  if (!context) throw new Error('Canvas2D is unavailable');
  const geometryCache = new WeakMap();
  const shades = new Map();
  const world = new Three.Matrix4();
  const instance = new Three.Matrix4();
  const viewProjection = new Three.Matrix4();
  const projection = new Three.Matrix4();
  const worldNormal = new Three.Matrix3();
  const normal = new Three.Vector3();
  const lightDirection = new Three.Vector3(-.4, .85, .38).normalize();
  const instanceColor = new Three.Color();
  const faces = [];
  let loopId = 0;
  let lastFrame = -Infinity;
  const near = .11;

  function prepareGeometry(original) {
    const cached = geometryCache.get(original);
    if (cached) return cached;
    const p = original.parameters || {};
    let geometry = original;
    if (original.type === 'SphereGeometry') {
      geometry = new Three.SphereGeometry(p.radius, 9, 6);
    } else if (original.type === 'CapsuleGeometry') {
      geometry = new Three.CapsuleGeometry(p.radius, p.length, 3, 7);
    } else if (original.type === 'TorusGeometry') {
      geometry = new Three.TorusGeometry(p.radius, p.tube, 5, 16, p.arc);
    } else if (original.type === 'TubeGeometry') {
      geometry = new Three.TubeGeometry(p.path, 8, p.radius, 4, p.closed);
    }
    const position = geometry.attributes.position;
    const index = geometry.index;
    const quads = index && (geometry.type === 'BoxGeometry' || geometry.type === 'PlaneGeometry')
      && (geometry.parameters.widthSegments === 1)
      && (geometry.parameters.heightSegments === 1)
      && (geometry.parameters.depthSegments === undefined || geometry.parameters.depthSegments === 1);
    const polygons = [];
    const a = new Three.Vector3();
    const b = new Three.Vector3();
    const c = new Three.Vector3();
    const edgeA = new Three.Vector3();
    const edgeB = new Three.Vector3();
    for (let i = 0; i < (index?.count ?? position.count); i += quads ? 6 : 3) {
      const vertices = [];
      const count = quads ? 6 : 3;
      for (let offset = 0; offset < count; offset++) {
        const vertex = index ? index.getX(i + offset) : i + offset;
        if (!vertices.includes(vertex)) vertices.push(vertex);
      }
      if (quads) {
        // The two triangles share an edge. Sort the quad corners around its center.
        a.fromBufferAttribute(position, vertices[0]);
        b.fromBufferAttribute(position, vertices[1]);
        c.fromBufferAttribute(position, vertices[2]);
        edgeA.subVectors(b, a);
        edgeB.subVectors(c, a).cross(edgeA).negate().normalize();
        edgeA.normalize();
        const tangent = edgeB.clone().cross(edgeA);
        const center = new Three.Vector3();
        for (const vertex of vertices) center.add(normal.fromBufferAttribute(position, vertex));
        center.multiplyScalar(.25);
        const angle = vertex => {
          normal.fromBufferAttribute(position, vertex).sub(center);
          return Math.atan2(normal.dot(tangent), normal.dot(edgeA));
        };
        vertices.sort((aIndex, bIndex) => angle(aIndex) - angle(bIndex));
      }
      a.fromBufferAttribute(position, vertices[0]);
      b.fromBufferAttribute(position, vertices[1]);
      c.fromBufferAttribute(position, vertices[2]);
      const direction = edgeA.subVectors(b, a).cross(edgeB.subVectors(c, a)).normalize().clone();
      polygons.push({ vertices, direction });
    }
    const prepared = { position, polygons, clip: new Float32Array(position.count * 4) };
    geometryCache.set(original, prepared);
    return prepared;
  }

  function projectedVertices(position, clip) {
    const e = projection.elements;
    for (let i = 0; i < position.count; i++) {
      const x = position.getX(i);
      const y = position.getY(i);
      const z = position.getZ(i);
      const offset = i * 4;
      clip[offset] = e[0] * x + e[4] * y + e[8] * z + e[12];
      clip[offset + 1] = e[1] * x + e[5] * y + e[9] * z + e[13];
      clip[offset + 2] = e[2] * x + e[6] * y + e[10] * z + e[14];
      clip[offset + 3] = e[3] * x + e[7] * y + e[11] * z + e[15];
    }
  }

  // Clip the polygon where it crosses the perspective near plane before dividing by w.
  function projectFace(clip, vertices) {
    const points = [];
    let depth = 0;
    for (let j = 0; j < vertices.length; j++) {
      const i = vertices[j] * 4;
      const k = vertices[(j + 1) % vertices.length] * 4;
      const w = clip[i + 3];
      const nextW = clip[k + 3];
      if (w > near) {
        points.push(clip[i] / w, clip[i + 1] / w);
        depth += w;
      }
      if ((w > near) !== (nextW > near)) {
        const mix = (near - w) / (nextW - w);
        points.push((clip[i] + (clip[k] - clip[i]) * mix) / near,
          (clip[i + 1] + (clip[k + 1] - clip[i + 1]) * mix) / near);
        depth += near;
      }
    }
    if (points.length < 6) return null;
    let area = 0;
    let minX = Infinity;
    let maxX = -Infinity;
    let minY = Infinity;
    let maxY = -Infinity;
    for (let i = 0; i < points.length; i += 2) {
      const x = points[i];
      const y = points[i + 1];
      const j = (i + 2) % points.length;
      area += x * points[j + 1] - y * points[j];
      minX = Math.min(minX, x);
      maxX = Math.max(maxX, x);
      minY = Math.min(minY, y);
      maxY = Math.max(maxY, y);
    }
    if (Math.abs(area) < .000003 || minX > 1 || maxX < -1 || minY > 1 || maxY < -1) return null;
    return { points, depth: (depth * 2) / points.length, area };
  }

  function faceColor(base, lighting) {
    const shade = Math.round(lighting * 14);
    const key = base * 32 + shade;
    if (!shades.has(key)) {
      const factor = shade / 14;
      const r = Math.min(255, Math.round((base >> 16) * factor));
      const g = Math.min(255, Math.round(((base >> 8) & 255) * factor));
      const b = Math.min(255, Math.round((base & 255) * factor));
      shades.set(key, `rgb(${r},${g},${b})`);
    }
    return shades.get(key);
  }

  function addMesh(mesh, material, matrix, color, far) {
    const { position, polygons, clip } = prepareGeometry(mesh.geometry);
    projection.multiplyMatrices(viewProjection, matrix);
    projectedVertices(position, clip);
    worldNormal.getNormalMatrix(matrix);
    const opacity = material.transparent ? material.opacity : 1;
    if (!opacity) return;
    for (const polygon of polygons) {
      const face = projectFace(clip, polygon.vertices);
      if (!face || face.depth > far) continue;
      if (face.area < 0 && material.side === Three.FrontSide) continue;
      if (face.area > 0 && material.side === Three.BackSide) continue;
      const incidence = normal.copy(polygon.direction).applyNormalMatrix(worldNormal).dot(lightDirection);
      const lit = material.isMeshBasicMaterial ? 1 :
        Math.min(1.22, .68 + Math.max(0, incidence) * .38 + Math.min(.15, (material.emissiveIntensity || 0) * .12));
      face.color = faceColor(color, lit);
      face.opacity = opacity;
      // The outdoor plane spans the view and sits below every modeled object.
      if (mesh.geometry.type === 'PlaneGeometry' && mesh.geometry.parameters.width > 30) face.depth = 1000;
      const dimensions = mesh.geometry.parameters;
      if (mesh.geometry.type === 'BoxGeometry' && dimensions.width > 10 &&
          dimensions.depth > 8 && dimensions.height <= .12) face.depth = 900;
      if (mesh.userData.paintDepth !== undefined) face.depth = mesh.userData.paintDepth;
      faces.push(face);
    }
  }

  function render(scene, camera) {
    scene.updateMatrixWorld();
    camera.updateMatrixWorld();
    viewProjection.multiplyMatrices(camera.projectionMatrix, camera.matrixWorldInverse);
    const { width, height } = canvas;
    context.clearRect(0, 0, width, height);
    if (scene.background?.isColor) {
      context.fillStyle = scene.background.getStyle();
      context.fillRect(0, 0, width, height);
    }
    faces.length = 0;
    scene.traverseVisible(node => {
      const material = node.material;
      if (!node.isMesh || !material?.visible || !material.color) return;
      if (node.isInstancedMesh) {
        for (let i = 0; i < node.count; i++) {
          node.getMatrixAt(i, instance);
          world.multiplyMatrices(node.matrixWorld, instance);
          if (node.instanceColor) node.getColorAt(i, instanceColor);
          addMesh(node, material, world, node.instanceColor ? instanceColor.getHex() : material.color.getHex(), camera.far);
        }
      } else {
        addMesh(node, material, node.matrixWorld, material.color.getHex(), camera.far);
      }
    });
    faces.sort((a, b) => b.depth - a.depth);
    for (const { points, color, opacity } of faces) {
      context.beginPath();
      context.moveTo((points[0] + 1) * width * .5, (1 - points[1]) * height * .5);
      for (let i = 2; i < points.length; i += 2) {
        context.lineTo((points[i] + 1) * width * .5, (1 - points[i + 1]) * height * .5);
      }
      context.closePath();
      context.fillStyle = color;
      context.globalAlpha = opacity;
      context.fill();
    }
    context.globalAlpha = 1;
  }

  return {
    domElement: canvas,
    outputColorSpace: Three.SRGBColorSpace,
    setPixelRatio() {},
    setSize(width, height) {
      const scale = Math.min(1, 960 / width, 720 / height);
      canvas.width = Math.max(1, Math.round(width * scale));
      canvas.height = Math.max(1, Math.round(height * scale));
    },
    render,
    setAnimationLoop(callback) {
      cancelAnimationFrame(loopId);
      if (!callback) return;
      function frame(time) {
        if (time - lastFrame > 65) {
          lastFrame = time;
          callback(time);
        }
        loopId = requestAnimationFrame(frame);
      }
      loopId = requestAnimationFrame(frame);
    },
  };
}

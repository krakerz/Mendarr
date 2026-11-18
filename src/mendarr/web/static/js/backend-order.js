// Backend switch order editor (Settings > Solver): draggable pills that keep the hidden
// order_1..order_3 inputs in sync. Pointer events rather than HTML drag and drop, so touch works too.
(function () {
  var root = document.getElementById("order-pills");
  if (!root) return;
  var all = (root.dataset.all || "").split(",").filter(Boolean);
  var max = 3;
  var ids = (root.dataset.order || "").split(",").filter(function (b) { return all.indexOf(b) >= 0; });
  if (!ids.length) ids = all.slice(0, max);
  var pills = root.querySelector(".order-pills");
  var spare = root.querySelector(".order-spare");
  var form = root.closest("form");
  var sprite = root.dataset.sprite || "/ui/static/vendor/lucide/sprite.svg";

  function icon(name) {
    return '<svg class="icon" aria-hidden="true" focusable="false"><use href="' + sprite + '#' + name + '"></use></svg>';
  }
  function esc(s) {
    return s.replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; });
  }

  function render() {
    pills.innerHTML = ids.map(function (id, i) {
      return '<span class="order-pill" data-id="' + esc(id) + '">'
        + '<span class="order-grip">' + icon("grip-vertical") + '</span><span class="order-pos">' + (i + 1) + '</span>' + esc(id)
        + '<button type="button" class="order-btn" data-move="-1" title="Earlier" aria-label="Move ' + esc(id) + ' earlier"' + (i === 0 ? " disabled" : "") + ">" + icon("chevron-left") + "</button>"
        + '<button type="button" class="order-btn" data-move="1" title="Later" aria-label="Move ' + esc(id) + ' later"' + (i === ids.length - 1 ? " disabled" : "") + ">" + icon("chevron-right") + "</button>"
        + '<button type="button" class="order-btn" data-remove="1" title="Take out" aria-label="Take ' + esc(id) + ' out"' + (ids.length === 1 ? " disabled" : "") + ">" + icon("x") + "</button></span>";
    }).join("");
    spare.innerHTML = all.filter(function (id) { return ids.indexOf(id) < 0; }).map(function (id) {
      return '<button type="button" class="order-add" data-add="' + esc(id) + '">' + icon("plus") + esc(id) + "</button>";
    }).join("");
    for (var n = 1; n <= max; n++) {
      var input = form.querySelector('input[name="order_' + n + '"]');
      if (input) input.value = ids[n - 1] || "";
    }
  }

  root.addEventListener("click", function (e) {
    var b = e.target.closest("button");
    if (!b || b.disabled) return;
    var pill = b.closest(".order-pill");
    var at = pill ? ids.indexOf(pill.dataset.id) : -1;
    if (b.hasAttribute("data-move") && at >= 0) {
      ids.splice(at + parseInt(b.getAttribute("data-move"), 10), 0, ids.splice(at, 1)[0]);
    } else if (b.hasAttribute("data-remove") && at >= 0 && ids.length > 1) {
      ids.splice(at, 1);
    } else if (b.hasAttribute("data-add") && ids.length < max) {
      ids.push(b.getAttribute("data-add"));
    } else {
      return;
    }
    render();
  });

  var drag = null;
  function clearMarks() {
    Array.prototype.forEach.call(root.querySelectorAll(".drop-before, .drop-after"), function (c) {
      c.classList.remove("drop-before", "drop-after");
    });
  }
  function targetAt(e) {
    var under = document.elementFromPoint(e.clientX, e.clientY);
    var pill = under && under.closest(".order-pill");
    return pill && pill !== drag.pill && root.contains(pill) ? pill : null;
  }
  function after(pill, e) {
    var box = pill.getBoundingClientRect();
    return e.clientX > box.left + box.width / 2;
  }

  root.addEventListener("pointerdown", function (e) {
    var pill = e.target.closest(".order-pill");
    if (!pill || e.target.closest("button") || e.button > 0) return;
    drag = { pill: pill, x: e.clientX, y: e.clientY, moving: false, pointer: e.pointerId };
  });
  root.addEventListener("pointermove", function (e) {
    if (!drag || e.pointerId !== drag.pointer) return;
    if (!drag.moving) {
      if (Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) < 6) return;
      drag.moving = true;
      drag.pill.classList.add("dragging");
      drag.pill.setPointerCapture(e.pointerId);
    }
    e.preventDefault();
    clearMarks();
    var t = targetAt(e);
    if (t) t.classList.add(after(t, e) ? "drop-after" : "drop-before");
  });
  function endDrag(e, drop) {
    if (!drag || e.pointerId !== drag.pointer) return;
    var done = drag;
    var t = done.moving && drop ? targetAt(e) : null;
    drag = null;
    done.pill.classList.remove("dragging");
    clearMarks();
    if (!t) return;
    var id = done.pill.dataset.id;
    ids.splice(ids.indexOf(id), 1);
    var to = ids.indexOf(t.dataset.id);
    ids.splice(after(t, e) ? to + 1 : to, 0, id);
    render();
  }
  root.addEventListener("pointerup", function (e) { endDrag(e, true); });
  root.addEventListener("pointercancel", function (e) { endDrag(e, false); });

  render();
})();

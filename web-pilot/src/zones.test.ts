import { describe, expect, it } from "vitest";
import {
  type Ed269Zone,
  type LonLat,
  type Shape,
  circleRing,
  closeRing,
  distanceM,
  emptyForm,
  featureFromForm,
  formFromFeature,
  limitsText,
  simpleRing,
  zoneStyle,
} from "./zones";

const SQUARE: LonLat[] = [
  [44.8, 41.7],
  [44.81, 41.7],
  [44.81, 41.71],
  [44.8, 41.71],
];
const POLYGON: Shape = { kind: "polygon", points: SQUARE };

function form(changes: Partial<ReturnType<typeof emptyForm>> = {}) {
  return { ...emptyForm("GEO"), identifier: "ED001", ...changes };
}

describe("featureFromForm", () => {
  it("writes an ED-269 zone with a closed ring and the limits as typed", () => {
    const { feature, errors } = featureFromForm(form({ name: "Drawn" }), POLYGON);
    expect(errors).toEqual([]);
    expect(feature).toEqual({
      type: "COMMON",
      identifier: "ED001",
      country: "GEO",
      name: "Drawn",
      restriction: "PROHIBITED",
      applicability: [{ permanent: "YES" }],
      zoneAuthority: [],
      geometry: [
        {
          uomDimensions: "M",
          lowerLimit: 0,
          lowerVerticalReference: "AGL",
          upperLimit: 120,
          upperVerticalReference: "AGL",
          horizontalProjection: {
            type: "Polygon",
            coordinates: [[...SQUARE, SQUARE[0]]],
          },
        },
      ],
    });
  });

  it("writes a circle as a circle, and an empty limit as unbounded", () => {
    const circle: Shape = { kind: "circle", center: [44.8, 41.7], radius: 500 };
    const { feature } = featureFromForm(form({ lowerLimit: "", uom: "FT" }), circle);
    const volume = feature?.geometry[0];
    expect(volume?.horizontalProjection).toEqual({
      type: "Circle",
      center: [44.8, 41.7],
      radius: 500,
    });
    expect(volume && "lowerLimit" in volume).toBe(false);
    expect(volume?.uomDimensions).toBe("FT");
  });

  it("writes a time window and a weekly schedule in UTC", () => {
    const { feature, errors } = featureFromForm(
      form({
        permanent: false,
        start: "2026-10-01T08:00",
        end: "2026-12-31T18:00",
        scheduleDays: ["SAT", "MON"],
        scheduleStart: "22:00",
        scheduleEnd: "02:00",
      }),
      POLYGON,
    );
    expect(errors).toEqual([]);
    expect(feature?.applicability).toEqual([
      {
        permanent: "NO",
        startDateTime: "2026-10-01T08:00:00Z",
        endDateTime: "2026-12-31T18:00:00Z",
        // In week order, whatever order they were ticked in.
        schedule: [{ day: ["MON", "SAT"], startTime: "22:00Z", endTime: "02:00Z" }],
      },
    ]);
  });

  it("writes every day as ANY", () => {
    const { feature } = featureFromForm(
      form({
        permanent: false,
        scheduleDays: ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
        scheduleStart: "08:00",
        scheduleEnd: "18:00",
      }),
      POLYGON,
    );
    expect(feature?.applicability[0]?.schedule?.[0]?.day).toEqual(["ANY"]);
  });

  it("names each field that is wrong, and writes nothing", () => {
    const { feature, errors } = featureFromForm(
      form({
        identifier: "TOOLONG1",
        country: "ge",
        lowerLimit: "200",
        upperLimit: "120",
        permanent: false,
      }),
      { kind: "polygon", points: SQUARE.slice(0, 2) },
    );
    expect(feature).toBeNull();
    expect(errors.map((e) => e.field)).toEqual([
      "identifier",
      "country",
      "upperLimit",
      "geometry",
      "applicability",
    ]);
  });

  it("allows a lower limit above the upper one in another reference", () => {
    const { errors } = featureFromForm(
      form({ lowerLimit: "200", lowerReference: "AMSL", upperReference: "AGL" }),
      POLYGON,
    );
    expect(errors).toEqual([]);
  });

  it("refuses a missing shape, a half schedule and a reversed window", () => {
    expect(featureFromForm(form(), null).errors.map((e) => e.key)).toEqual(["zone_error_no_shape"]);
    const half = featureFromForm(form({ permanent: false, scheduleDays: ["MON"] }), POLYGON);
    expect(half.errors.map((e) => e.key)).toEqual(["zone_error_schedule"]);
    const reversed = featureFromForm(
      form({ permanent: false, start: "2026-10-02T00:00", end: "2026-10-01T00:00" }),
      POLYGON,
    );
    expect(reversed.errors.map((e) => e.key)).toEqual(["zone_error_window"]);
  });
});

describe("formFromFeature", () => {
  const published: Ed269Zone = {
    identifier: "TST002",
    country: "GEO",
    type: "COMMON",
    restriction: "REQ_AUTHORISATION",
    reason: ["POPULATION"],
    applicability: [
      {
        permanent: "NO",
        startDateTime: "2026-01-01T00:00:00Z",
        endDateTime: "2027-01-01T00:00:00Z",
      },
    ],
    zoneAuthority: [{ name: "Test authority", purpose: "NOTIFICATION" }],
    geometry: [
      {
        uomDimensions: "M",
        lowerVerticalReference: "AGL",
        upperLimit: 120,
        upperVerticalReference: "AGL",
        horizontalProjection: {
          type: "Polygon",
          coordinates: [
            [...SQUARE, SQUARE[0] as LonLat],
            [
              [44.802, 41.702],
              [44.804, 41.702],
              [44.804, 41.704],
              [44.802, 41.702],
            ],
          ],
        },
      },
    ],
    region: 7,
    extendedProperties: { source: "test" },
  };

  it("gives back the zone it was made from, holes and extras included", () => {
    const { form: edited, shape } = formFromFeature(published);
    const { feature, errors } = featureFromForm(edited, shape);
    expect(errors).toEqual([]);
    expect(feature).toEqual(published);
  });

  it("reads a circle as a circle", () => {
    const circle: Ed269Zone = {
      ...published,
      geometry: [
        {
          uomDimensions: "FT",
          lowerVerticalReference: "AMSL",
          upperVerticalReference: "AMSL",
          horizontalProjection: { type: "Circle", center: [44.9, 41.75], radius: 250.5 },
        },
      ],
    };
    const { form: edited, shape } = formFromFeature(circle);
    expect(shape).toEqual({ kind: "circle", center: [44.9, 41.75], radius: 250.5 });
    expect(featureFromForm(edited, shape).feature).toEqual(circle);
  });
});

describe("simpleRing", () => {
  it("accepts a square, closed or open", () => {
    expect(simpleRing(SQUARE)).toBe(true);
    expect(simpleRing(closeRing(SQUARE))).toBe(true);
  });

  it("refuses a bow tie and collinear corners", () => {
    const bowTie: LonLat[] = [
      [44.8, 41.7],
      [44.81, 41.71],
      [44.81, 41.7],
      [44.8, 41.71],
    ];
    const collinear: LonLat[] = [
      [44.8, 41.7],
      [44.805, 41.7],
      [44.81, 41.7],
    ];
    expect(simpleRing(bowTie)).toBe(false);
    expect(simpleRing(collinear)).toBe(false);
  });

  it("refuses a ring whose edge runs back along another", () => {
    const spike: LonLat[] = [
      [44.8, 41.7],
      [44.81, 41.7],
      [44.81, 41.71],
      [44.805, 41.7],
    ];
    expect(simpleRing(spike)).toBe(false);
  });

  it("is what the form says about a drawn bow tie", () => {
    const { errors } = featureFromForm(form(), {
      kind: "polygon",
      points: [
        [44.8, 41.7],
        [44.81, 41.71],
        [44.81, 41.7],
        [44.8, 41.71],
      ],
    });
    expect(errors.map((e) => e.key)).toEqual(["zone_error_self_intersecting"]);
  });
});

describe("geometry helpers", () => {
  it("closes a ring once", () => {
    expect(closeRing(SQUARE)).toHaveLength(5);
    expect(closeRing(closeRing(SQUARE))).toHaveLength(5);
    expect(closeRing([])).toEqual([]);
  });

  it("measures a great-circle distance", () => {
    // One minute of latitude is about 1852 m.
    expect(distanceM([44.8, 41.7], [44.8, 41.7 + 1 / 60])).toBeCloseTo(1853, -1);
  });

  it("draws a circle whose vertices are on it", () => {
    const ring = circleRing([44.8, 41.7], 1000);
    expect(ring).toHaveLength(65);
    for (const point of ring) expect(distanceM([44.8, 41.7], point)).toBeCloseTo(1000, 3);
  });
});

describe("zoneStyle", () => {
  const zone = (restriction: Ed269Zone["restriction"], active: boolean, type = "geozone") => ({
    type,
    active_now: active,
    feature: { restriction } as Ed269Zone,
  });

  it("colours by restriction and fades an inactive zone", () => {
    expect(zoneStyle(zone("PROHIBITED", true))).toEqual({
      colour: "#c62828",
      fillOpacity: 0.18,
      dashed: false,
    });
    expect(zoneStyle(zone("PROHIBITED", false)).dashed).toBe(true);
    expect(zoneStyle(zone("CONDITIONAL", true)).colour).not.toBe(
      zoneStyle(zone("REQ_AUTHORISATION", true)).colour,
    );
    expect(zoneStyle(zone("NO_RESTRICTION", true, "base")).colour).toBe("#1565c0");
  });
});

describe("limitsText", () => {
  it("names the surface and no ceiling when a limit is absent", () => {
    expect(
      limitsText(
        {
          uomDimensions: "FT",
          lowerVerticalReference: "AGL",
          upperLimit: 400,
          upperVerticalReference: "AMSL",
          horizontalProjection: { type: "Circle", center: [0, 0], radius: 1 },
        },
        "surface",
        "unlimited",
      ),
    ).toBe("surface – 400 ft AMSL");
  });
});

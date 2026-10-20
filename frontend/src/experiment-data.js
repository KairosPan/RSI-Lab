// Generated from immutable experiment artifacts by scripts/import-experiment.mjs.
// This is a completed-run readout, not live telemetry. Do not edit by hand.
export const experiment = {
  "id": "lab_comparison_v2",
  "title": "Prospective CDK2 comparison",
  "target": "Cyclin-dependent kinase 2",
  "assayId": "CHEMBL1250281",
  "generatedAt": "2026-10-20T07:17:46.819963+00:00",
  "status": "completed",
  "poolSize": 20,
  "shortlistSize": 5,
  "model": "gpt-6-astra",
  "effort": "ultra",
  "protocolId": "lab_cdk2_gnina_p0profile_comparison_v2",
  "protocolHash": "69f1ee145a3bbced30d0e717d35dee296c4d2b0c823ce04f3bb848e9ae8637e0",
  "limits": {
    "onlineCandidates": 8,
    "seedsPerCandidate": 2,
    "batchCandidates": 4,
    "shortlistSize": 5,
    "modelCalls": 6,
    "modelCallSeconds": 300,
    "tokenSoftLimit": 150000,
    "terminalCandidates": 5,
    "terminalFeedback": false
  },
  "arms": [
    {
      "id": "baseline",
      "label": "Fixed baseline",
      "status": "completed",
      "presentationMode": "fixed_ic50_top8",
      "completedAt": "2026-10-20T06:32:08.649360+00:00",
      "shortlist": [
        "CHEMBL50894",
        "CHEMBL194721",
        "CHEMBL1241566",
        "CHEMBL1207227",
        "CHEMBL1241567"
      ],
      "quality": {
        "validCount": 5,
        "shortlistSize": 5,
        "meanCnn": 0.97060296535,
        "meanEnergy": -9.286010000000001,
        "meanCnnRange": 0.007136535580000025,
        "maxCnnRange": 0.027980029500000003
      },
      "usage": {
        "calls": 0,
        "inputTokens": 0,
        "outputTokens": 0,
        "totalTokens": 0,
        "llmWallSeconds": 0,
        "costUsd": null
      },
      "numerical": {
        "onlineCandidates": 8,
        "jobs": 16,
        "completedJobs": 16,
        "poseValidJobs": 13,
        "batchWallSeconds": 511.468545124,
        "cpuSeconds": 745.6700139999999,
        "gpuObservedJobs": 16,
        "terminalJobs": 0
      },
      "memoryCount": 0,
      "pendingMemoryCount": 0,
      "source": "runs/lab_comparison_v2/fixed_baseline/evaluation.json"
    },
    {
      "id": "H0",
      "label": "H0 · Expanded JSON",
      "status": "completed",
      "presentationMode": "expanded_json_v1",
      "completedAt": "2026-10-20T06:53:14.810618+00:00",
      "shortlist": [
        "CHEMBL50894",
        "CHEMBL194721",
        "CHEMBL1241566",
        "CHEMBL1207227",
        "CHEMBL1241567"
      ],
      "quality": {
        "validCount": 5,
        "shortlistSize": 5,
        "meanCnn": 0.97060296535,
        "meanEnergy": -9.286010000000001,
        "meanCnnRange": 0.007136535580000025,
        "maxCnnRange": 0.027980029500000003
      },
      "usage": {
        "calls": 4,
        "inputTokens": 145222,
        "outputTokens": 16874,
        "totalTokens": 162096,
        "llmWallSeconds": 417.65253249986563,
        "costUsd": null
      },
      "numerical": {
        "onlineCandidates": 8,
        "jobs": 16,
        "completedJobs": 16,
        "poseValidJobs": 13,
        "batchWallSeconds": 531.1978152229999,
        "cpuSeconds": 762.6626940000001,
        "gpuObservedJobs": 16,
        "terminalJobs": 0
      },
      "memoryCount": 9,
      "pendingMemoryCount": 9,
      "source": "runs/lab_comparison_v2/H0/evaluation.json"
    },
    {
      "id": "H1",
      "label": "H1 · Compact JSON candidate",
      "status": "completed",
      "presentationMode": "compact_json_v1",
      "completedAt": "2026-10-20T07:14:55.802665+00:00",
      "shortlist": [
        "CHEMBL50894",
        "CHEMBL194721",
        "CHEMBL258805",
        "CHEMBL1207227",
        "CHEMBL1241567"
      ],
      "quality": {
        "validCount": 5,
        "shortlistSize": 5,
        "meanCnn": 0.9714147925399998,
        "meanEnergy": -8.943037,
        "meanCnnRange": 0.0071798324000000415,
        "maxCnnRange": 0.027980029500000003
      },
      "usage": {
        "calls": 4,
        "inputTokens": 121110,
        "outputTokens": 16411,
        "totalTokens": 137521,
        "llmWallSeconds": 444.784498960129,
        "costUsd": null
      },
      "numerical": {
        "onlineCandidates": 8,
        "jobs": 16,
        "completedJobs": 16,
        "poseValidJobs": 13,
        "batchWallSeconds": 502.41538746100014,
        "cpuSeconds": 711.5343849999999,
        "gpuObservedJobs": 16,
        "terminalJobs": 0
      },
      "memoryCount": 8,
      "pendingMemoryCount": 8,
      "source": "runs/lab_comparison_v2/H1/evaluation.json"
    }
  ],
  "candidates": [
    {
      "id": "CHEMBL102926",
      "smiles": "Cc1nc2ccccn2c1-c1ccnc(Nc2ccc(OCC(O)CN(C)C)cc2)n1",
      "ic50Nm": 1,
      "assayId": "CHEMBL1250281",
      "scaffold": "scaf_f1afc3989eae",
      "activityIds": [
        3446964
      ],
      "documentIds": [
        "CHEMBL1240533"
      ],
      "results": {
        "baseline": {
          "completedRepeats": 2,
          "validRepeats": 0,
          "pairedValid": false,
          "cnnMedian": null,
          "energyMedian": null,
          "cnnRange": 0.04895198349999996,
          "shortlisted": false,
          "rank": null,
          "evidenceId": "baseline-calculation-CHEMBL102926",
          "source": "runs/lab_comparison_v2/fixed_baseline/state.json",
          "jobs": [
            {
              "seed": 17,
              "status": "completed",
              "cnnScore": 0.9266286492,
              "energy": -8.67488,
              "cnnAffinity": 7.9868850708,
              "poseValid": false,
              "identityPreserved": false,
              "failedChecks": [],
              "cpuSeconds": 67.656649,
              "wallSeconds": 47.05838598999992,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_001/jobs/CHEMBL102926_s17/result.json"
            },
            {
              "seed": 43,
              "status": "completed",
              "cnnScore": 0.9755806327,
              "energy": -8.15749,
              "cnnAffinity": 7.8927283287,
              "poseValid": false,
              "identityPreserved": false,
              "failedChecks": [],
              "cpuSeconds": 66.721529,
              "wallSeconds": 46.43906837899999,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_001/jobs/CHEMBL102926_s43/result.json"
            }
          ]
        },
        "H0": {
          "completedRepeats": 2,
          "validRepeats": 0,
          "pairedValid": false,
          "cnnMedian": null,
          "energyMedian": null,
          "cnnRange": 0.04895198349999996,
          "shortlisted": false,
          "rank": null,
          "evidenceId": "H0-ev_tool_s0_gnina_01_CHEMBL102926",
          "source": "runs/lab_comparison_v2/H0/state.json",
          "jobs": [
            {
              "seed": 17,
              "status": "completed",
              "cnnScore": 0.9266286492,
              "energy": -8.67488,
              "cnnAffinity": 7.9868850708,
              "poseValid": false,
              "identityPreserved": false,
              "failedChecks": [],
              "cpuSeconds": 72.64861499999999,
              "wallSeconds": 50.041647323999996,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H0/state.json"
            },
            {
              "seed": 43,
              "status": "completed",
              "cnnScore": 0.9755806327,
              "energy": -8.15749,
              "cnnAffinity": 7.8927283287,
              "poseValid": false,
              "identityPreserved": false,
              "failedChecks": [],
              "cpuSeconds": 70.65006500000001,
              "wallSeconds": 48.87615139900004,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H0/state.json"
            }
          ]
        },
        "H1": {
          "completedRepeats": 2,
          "validRepeats": 0,
          "pairedValid": false,
          "cnnMedian": null,
          "energyMedian": null,
          "cnnRange": 0.04895198349999996,
          "shortlisted": false,
          "rank": null,
          "evidenceId": "H1-ev_tool_s0_gnina_batch_01_CHEMBL102926",
          "source": "runs/lab_comparison_v2/H1/state.json",
          "jobs": [
            {
              "seed": 17,
              "status": "completed",
              "cnnScore": 0.9266286492,
              "energy": -8.67488,
              "cnnAffinity": 7.9868850708,
              "poseValid": false,
              "identityPreserved": false,
              "failedChecks": [],
              "cpuSeconds": 68.031808,
              "wallSeconds": 47.06095069599996,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H1/state.json"
            },
            {
              "seed": 43,
              "status": "completed",
              "cnnScore": 0.9755806327,
              "energy": -8.15749,
              "cnnAffinity": 7.8927283287,
              "poseValid": false,
              "identityPreserved": false,
              "failedChecks": [],
              "cpuSeconds": 68.165551,
              "wallSeconds": 47.095059841999955,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H1/state.json"
            }
          ]
        }
      }
    },
    {
      "id": "CHEMBL194721",
      "smiles": "FC(F)(F)c1cnc(Nc2ccc3[nH]cnc3c2)nc1Nc1ccccc1Cl",
      "ic50Nm": 2,
      "assayId": "CHEMBL1250281",
      "scaffold": "scaf_0bf6483a41f3",
      "activityIds": [
        3446973
      ],
      "documentIds": [
        "CHEMBL1240533"
      ],
      "results": {
        "baseline": {
          "completedRepeats": 2,
          "validRepeats": 2,
          "pairedValid": true,
          "cnnMedian": 0.99269136785,
          "energyMedian": -9.114985,
          "cnnRange": 0.004059851100000067,
          "shortlisted": true,
          "rank": 2,
          "evidenceId": "baseline-calculation-CHEMBL194721",
          "source": "runs/lab_comparison_v2/fixed_baseline/state.json",
          "jobs": [
            {
              "seed": 17,
              "status": "completed",
              "cnnScore": 0.9947212934,
              "energy": -9.30373,
              "cnnAffinity": 7.18116045,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 45.769684000000005,
              "wallSeconds": 32.99960110699999,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_001/jobs/CHEMBL194721_s17/result.json"
            },
            {
              "seed": 43,
              "status": "completed",
              "cnnScore": 0.9906614423,
              "energy": -8.92624,
              "cnnAffinity": 7.2469830513,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 46.990868,
              "wallSeconds": 32.180950518000145,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_001/jobs/CHEMBL194721_s43/result.json"
            }
          ]
        },
        "H0": {
          "completedRepeats": 2,
          "validRepeats": 2,
          "pairedValid": true,
          "cnnMedian": 0.99269136785,
          "energyMedian": -9.114985,
          "cnnRange": 0.004059851100000067,
          "shortlisted": true,
          "rank": 2,
          "evidenceId": "H0-ev_tool_s0_gnina_01_CHEMBL194721",
          "source": "runs/lab_comparison_v2/H0/state.json",
          "jobs": [
            {
              "seed": 17,
              "status": "completed",
              "cnnScore": 0.9947212934,
              "energy": -9.30373,
              "cnnAffinity": 7.18116045,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 46.522176,
              "wallSeconds": 33.91211764800005,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H0/state.json"
            },
            {
              "seed": 43,
              "status": "completed",
              "cnnScore": 0.9906614423,
              "energy": -8.92624,
              "cnnAffinity": 7.2469830513,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 48.256814,
              "wallSeconds": 30.04845338600012,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H0/state.json"
            }
          ]
        },
        "H1": {
          "completedRepeats": 2,
          "validRepeats": 2,
          "pairedValid": true,
          "cnnMedian": 0.99269136785,
          "energyMedian": -9.114985,
          "cnnRange": 0.004059851100000067,
          "shortlisted": true,
          "rank": 2,
          "evidenceId": "H1-ev_tool_s0_gnina_batch_01_CHEMBL194721",
          "source": "runs/lab_comparison_v2/H1/state.json",
          "jobs": [
            {
              "seed": 17,
              "status": "completed",
              "cnnScore": 0.9947212934,
              "energy": -9.30373,
              "cnnAffinity": 7.18116045,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 46.490281,
              "wallSeconds": 33.61312070300028,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H1/state.json"
            },
            {
              "seed": 43,
              "status": "completed",
              "cnnScore": 0.9906614423,
              "energy": -8.92624,
              "cnnAffinity": 7.2469830513,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 45.781691,
              "wallSeconds": 27.148944006999955,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H1/state.json"
            }
          ]
        }
      }
    },
    {
      "id": "CHEMBL1241849",
      "smiles": "CN(C)CCNS(=O)(=O)c1ccc(Nc2cccc(-c3cnc4ccccn34)c2)cc1",
      "ic50Nm": 5.000000000000001,
      "assayId": "CHEMBL1250281",
      "scaffold": "scaf_76d86bbf9f50",
      "activityIds": [
        3446971
      ],
      "documentIds": [
        "CHEMBL1240533"
      ],
      "results": {
        "baseline": {
          "completedRepeats": 2,
          "validRepeats": 2,
          "pairedValid": true,
          "cnnMedian": 0.64205130935,
          "energyMedian": -9.301580000000001,
          "cnnRange": 0.10435503729999995,
          "shortlisted": false,
          "rank": null,
          "evidenceId": "baseline-calculation-CHEMBL1241849",
          "source": "runs/lab_comparison_v2/fixed_baseline/state.json",
          "jobs": [
            {
              "seed": 17,
              "status": "completed",
              "cnnScore": 0.694228828,
              "energy": -9.08103,
              "cnnAffinity": 6.8536119461,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 69.49141,
              "wallSeconds": 45.783677876999946,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_001/jobs/CHEMBL1241849_s17/result.json"
            },
            {
              "seed": 43,
              "status": "completed",
              "cnnScore": 0.5898737907,
              "energy": -9.52213,
              "cnnAffinity": 6.9678077698,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 67.707283,
              "wallSeconds": 46.244579925999915,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_001/jobs/CHEMBL1241849_s43/result.json"
            }
          ]
        },
        "H0": {
          "completedRepeats": 2,
          "validRepeats": 2,
          "pairedValid": true,
          "cnnMedian": 0.64205130935,
          "energyMedian": -9.301580000000001,
          "cnnRange": 0.10435503729999995,
          "shortlisted": false,
          "rank": null,
          "evidenceId": "H0-ev_tool_s2_gnina_01_CHEMBL1241849",
          "source": "runs/lab_comparison_v2/H0/state.json",
          "jobs": [
            {
              "seed": 17,
              "status": "completed",
              "cnnScore": 0.694228828,
              "energy": -9.08103,
              "cnnAffinity": 6.8536119461,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 69.237651,
              "wallSeconds": 47.73175196400007,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H0/state.json"
            },
            {
              "seed": 43,
              "status": "completed",
              "cnnScore": 0.5898737907,
              "energy": -9.52213,
              "cnnAffinity": 6.9678077698,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 70.060393,
              "wallSeconds": 48.42985849099978,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H0/state.json"
            }
          ]
        },
        "H1": {
          "completedRepeats": 2,
          "validRepeats": 2,
          "pairedValid": true,
          "cnnMedian": 0.64205130935,
          "energyMedian": -9.301580000000001,
          "cnnRange": 0.10435503729999995,
          "shortlisted": false,
          "rank": null,
          "evidenceId": "H1-ev_tool_s2_gnina_batch_01_CHEMBL1241849",
          "source": "runs/lab_comparison_v2/H1/state.json",
          "jobs": [
            {
              "seed": 17,
              "status": "completed",
              "cnnScore": 0.694228828,
              "energy": -9.08103,
              "cnnAffinity": 6.8536119461,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 68.97403899999999,
              "wallSeconds": 47.678281522999896,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H1/state.json"
            },
            {
              "seed": 43,
              "status": "completed",
              "cnnScore": 0.5898737907,
              "energy": -9.52213,
              "cnnAffinity": 6.9678077698,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 67.56668499999999,
              "wallSeconds": 47.26617645399983,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H1/state.json"
            }
          ]
        }
      }
    },
    {
      "id": "CHEMBL1241567",
      "smiles": "CCCCOc1c(C(=O)c2c(F)cc(C)cc2F)ccc2[nH]ncc12",
      "ic50Nm": 6,
      "assayId": "CHEMBL1250281",
      "scaffold": "scaf_2b2dc6b2753d",
      "activityIds": [
        3446966
      ],
      "documentIds": [
        "CHEMBL1240533"
      ],
      "results": {
        "baseline": {
          "completedRepeats": 2,
          "validRepeats": 2,
          "pairedValid": true,
          "cnnMedian": 0.91990068555,
          "energyMedian": -7.829445,
          "cnnRange": 0.027980029500000003,
          "shortlisted": true,
          "rank": 5,
          "evidenceId": "baseline-calculation-CHEMBL1241567",
          "source": "runs/lab_comparison_v2/fixed_baseline/state.json",
          "jobs": [
            {
              "seed": 17,
              "status": "completed",
              "cnnScore": 0.9338907003,
              "energy": -7.31572,
              "cnnAffinity": 7.0732722282,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 44.452085,
              "wallSeconds": 27.8476080800001,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_001/jobs/CHEMBL1241567_s17/result.json"
            },
            {
              "seed": 43,
              "status": "completed",
              "cnnScore": 0.9059106708,
              "energy": -8.34317,
              "cnnAffinity": 7.0896420479,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 43.208194,
              "wallSeconds": 27.5672064649998,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_001/jobs/CHEMBL1241567_s43/result.json"
            }
          ]
        },
        "H0": {
          "completedRepeats": 2,
          "validRepeats": 2,
          "pairedValid": true,
          "cnnMedian": 0.91990068555,
          "energyMedian": -7.829445,
          "cnnRange": 0.027980029500000003,
          "shortlisted": true,
          "rank": 5,
          "evidenceId": "H0-ev_tool_s0_gnina_01_CHEMBL1241567",
          "source": "runs/lab_comparison_v2/H0/state.json",
          "jobs": [
            {
              "seed": 17,
              "status": "completed",
              "cnnScore": 0.9338907003,
              "energy": -7.31572,
              "cnnAffinity": 7.0732722282,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 44.938658999999994,
              "wallSeconds": 28.208291645999907,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H0/state.json"
            },
            {
              "seed": 43,
              "status": "completed",
              "cnnScore": 0.9059106708,
              "energy": -8.34317,
              "cnnAffinity": 7.0896420479,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 44.378932,
              "wallSeconds": 33.17753011800005,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H0/state.json"
            }
          ]
        },
        "H1": {
          "completedRepeats": 2,
          "validRepeats": 2,
          "pairedValid": true,
          "cnnMedian": 0.91990068555,
          "energyMedian": -7.829445,
          "cnnRange": 0.027980029500000003,
          "shortlisted": true,
          "rank": 5,
          "evidenceId": "H1-ev_tool_s0_gnina_batch_01_CHEMBL1241567",
          "source": "runs/lab_comparison_v2/H1/state.json",
          "jobs": [
            {
              "seed": 17,
              "status": "completed",
              "cnnScore": 0.9338907003,
              "energy": -7.31572,
              "cnnAffinity": 7.0732722282,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 42.73235,
              "wallSeconds": 26.06064790499977,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H1/state.json"
            },
            {
              "seed": 43,
              "status": "completed",
              "cnnScore": 0.9059106708,
              "energy": -8.34317,
              "cnnAffinity": 7.0896420479,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 43.28846,
              "wallSeconds": 31.849351643999853,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H1/state.json"
            }
          ]
        }
      }
    },
    {
      "id": "CHEMBL362560",
      "smiles": "COc1ccc(-c2[nH]nc3c2C(=O)c2c(NC(=O)NNC(=O)c4ccc(O)cc4)cccc2-3)cc1",
      "ic50Nm": 9,
      "assayId": "CHEMBL1250281",
      "scaffold": "scaf_5955612e6955",
      "activityIds": [
        3446970
      ],
      "documentIds": [
        "CHEMBL1240533"
      ],
      "results": {
        "baseline": {
          "completedRepeats": 2,
          "validRepeats": 1,
          "pairedValid": false,
          "cnnMedian": null,
          "energyMedian": null,
          "cnnRange": 0.3979472816,
          "shortlisted": false,
          "rank": null,
          "evidenceId": "baseline-calculation-CHEMBL362560",
          "source": "runs/lab_comparison_v2/fixed_baseline/state.json",
          "jobs": [
            {
              "seed": 17,
              "status": "completed",
              "cnnScore": 0.2729690969,
              "energy": -10.93711,
              "cnnAffinity": 6.9387550354,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 67.31528200000001,
              "wallSeconds": 45.643975573000034,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_002/jobs/CHEMBL362560_s17/result.json"
            },
            {
              "seed": 43,
              "status": "completed",
              "cnnScore": 0.6709163785,
              "energy": -12.1794,
              "cnnAffinity": 7.205634594,
              "poseValid": false,
              "identityPreserved": true,
              "failedChecks": [
                "internal_steric_clash",
                "internal_energy"
              ],
              "cpuSeconds": 70.039497,
              "wallSeconds": 45.41462274100013,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_002/jobs/CHEMBL362560_s43/result.json"
            }
          ]
        },
        "H0": {
          "completedRepeats": 2,
          "validRepeats": 1,
          "pairedValid": false,
          "cnnMedian": null,
          "energyMedian": null,
          "cnnRange": 0.3979472816,
          "shortlisted": false,
          "rank": null,
          "evidenceId": "H0-ev_tool_s0_gnina_01_CHEMBL362560",
          "source": "runs/lab_comparison_v2/H0/state.json",
          "jobs": [
            {
              "seed": 17,
              "status": "completed",
              "cnnScore": 0.2729690969,
              "energy": -10.93711,
              "cnnAffinity": 6.9387550354,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 68.189663,
              "wallSeconds": 47.00404958599984,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H0/state.json"
            },
            {
              "seed": 43,
              "status": "completed",
              "cnnScore": 0.6709163785,
              "energy": -12.1794,
              "cnnAffinity": 7.205634594,
              "poseValid": false,
              "identityPreserved": true,
              "failedChecks": [
                "internal_steric_clash",
                "internal_energy"
              ],
              "cpuSeconds": 70.74819,
              "wallSeconds": 48.395093231000146,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H0/state.json"
            }
          ]
        },
        "H1": {
          "completedRepeats": 2,
          "validRepeats": 1,
          "pairedValid": false,
          "cnnMedian": null,
          "energyMedian": null,
          "cnnRange": 0.3979472816,
          "shortlisted": false,
          "rank": null,
          "evidenceId": "H1-ev_tool_s2_gnina_batch_01_CHEMBL362560",
          "source": "runs/lab_comparison_v2/H1/state.json",
          "jobs": [
            {
              "seed": 17,
              "status": "completed",
              "cnnScore": 0.2729690969,
              "energy": -10.93711,
              "cnnAffinity": 6.9387550354,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 67.52559600000001,
              "wallSeconds": 46.27897854999992,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H1/state.json"
            },
            {
              "seed": 43,
              "status": "completed",
              "cnnScore": 0.6709163785,
              "energy": -12.1794,
              "cnnAffinity": 7.205634594,
              "poseValid": false,
              "identityPreserved": true,
              "failedChecks": [
                "internal_steric_clash",
                "internal_energy"
              ],
              "cpuSeconds": 69.283989,
              "wallSeconds": 47.406531408999854,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H1/state.json"
            }
          ]
        }
      }
    },
    {
      "id": "CHEMBL50894",
      "smiles": "O=C1Cc2c([nH]c3ccc([N+](=O)[O-])cc23)-c2ccccc2N1",
      "ic50Nm": 15.000000000000004,
      "assayId": "CHEMBL1250281",
      "scaffold": "scaf_4b988936ce17",
      "activityIds": [
        3446955
      ],
      "documentIds": [
        "CHEMBL1240533"
      ],
      "results": {
        "baseline": {
          "completedRepeats": 2,
          "validRepeats": 2,
          "pairedValid": true,
          "cnnMedian": 0.99664747715,
          "energyMedian": -9.98197,
          "cnnRange": 0.000005125900000013672,
          "shortlisted": true,
          "rank": 1,
          "evidenceId": "baseline-calculation-CHEMBL50894",
          "source": "runs/lab_comparison_v2/fixed_baseline/state.json",
          "jobs": [
            {
              "seed": 17,
              "status": "completed",
              "cnnScore": 0.9966449142,
              "energy": -9.97848,
              "cnnAffinity": 6.4814443588,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 16.60511,
              "wallSeconds": 11.324848820999932,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_002/jobs/CHEMBL50894_s17/result.json"
            },
            {
              "seed": 43,
              "status": "completed",
              "cnnScore": 0.9966500401,
              "energy": -9.98546,
              "cnnAffinity": 6.4787092209,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 17.577929,
              "wallSeconds": 11.93734404299994,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_002/jobs/CHEMBL50894_s43/result.json"
            }
          ]
        },
        "H0": {
          "completedRepeats": 2,
          "validRepeats": 2,
          "pairedValid": true,
          "cnnMedian": 0.99664747715,
          "energyMedian": -9.98197,
          "cnnRange": 0.000005125900000013672,
          "shortlisted": true,
          "rank": 1,
          "evidenceId": "H0-ev_tool_s2_gnina_01_CHEMBL50894",
          "source": "runs/lab_comparison_v2/H0/state.json",
          "jobs": [
            {
              "seed": 17,
              "status": "completed",
              "cnnScore": 0.9966449142,
              "energy": -9.97848,
              "cnnAffinity": 6.4814443588,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 17.026219,
              "wallSeconds": 11.778339497000161,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H0/state.json"
            },
            {
              "seed": 43,
              "status": "completed",
              "cnnScore": 0.9966500401,
              "energy": -9.98546,
              "cnnAffinity": 6.4787092209,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 17.821216,
              "wallSeconds": 12.053696676000072,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H0/state.json"
            }
          ]
        },
        "H1": {
          "completedRepeats": 2,
          "validRepeats": 2,
          "pairedValid": true,
          "cnnMedian": 0.99664747715,
          "energyMedian": -9.98197,
          "cnnRange": 0.000005125900000013672,
          "shortlisted": true,
          "rank": 1,
          "evidenceId": "H1-ev_tool_s0_gnina_batch_01_CHEMBL50894",
          "source": "runs/lab_comparison_v2/H1/state.json",
          "jobs": [
            {
              "seed": 17,
              "status": "completed",
              "cnnScore": 0.9966449142,
              "energy": -9.97848,
              "cnnAffinity": 6.4814443588,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 16.975073000000002,
              "wallSeconds": 11.6042181480002,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H1/state.json"
            },
            {
              "seed": 43,
              "status": "completed",
              "cnnScore": 0.9966500401,
              "energy": -9.98546,
              "cnnAffinity": 6.4787092209,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 18.857387,
              "wallSeconds": 18.3444059389999,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H1/state.json"
            }
          ]
        }
      }
    },
    {
      "id": "CHEMBL1207227",
      "smiles": "O=C1C(c2c(O)[nH]c3ccc(S(=O)(=O)O)cc23)=Nc2ccccc21",
      "ic50Nm": 21.999999999999996,
      "assayId": "CHEMBL1250281",
      "scaffold": "scaf_71dd6f433b2f",
      "activityIds": [
        3446954
      ],
      "documentIds": [
        "CHEMBL1240533"
      ],
      "results": {
        "baseline": {
          "completedRepeats": 2,
          "validRepeats": 2,
          "pairedValid": true,
          "cnnMedian": 0.9604794681,
          "energyMedian": -9.821745,
          "cnnRange": 0.00021630520000004427,
          "shortlisted": true,
          "rank": 4,
          "evidenceId": "baseline-calculation-CHEMBL1207227",
          "source": "runs/lab_comparison_v2/fixed_baseline/state.json",
          "jobs": [
            {
              "seed": 17,
              "status": "completed",
              "cnnScore": 0.9605876207,
              "energy": -9.97589,
              "cnnAffinity": 5.8956823349,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 27.257942,
              "wallSeconds": 22.505802640000184,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_002/jobs/CHEMBL1207227_s17/result.json"
            },
            {
              "seed": 43,
              "status": "completed",
              "cnnScore": 0.9603713155,
              "energy": -9.6676,
              "cnnAffinity": 5.8206458092,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 25.987678,
              "wallSeconds": 16.341637284999933,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_002/jobs/CHEMBL1207227_s43/result.json"
            }
          ]
        },
        "H0": {
          "completedRepeats": 2,
          "validRepeats": 2,
          "pairedValid": true,
          "cnnMedian": 0.9604794681,
          "energyMedian": -9.821745,
          "cnnRange": 0.00021630520000004427,
          "shortlisted": true,
          "rank": 4,
          "evidenceId": "H0-ev_tool_s2_gnina_01_CHEMBL1207227",
          "source": "runs/lab_comparison_v2/H0/state.json",
          "jobs": [
            {
              "seed": 17,
              "status": "completed",
              "cnnScore": 0.9605876207,
              "energy": -9.97589,
              "cnnAffinity": 5.8956823349,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 26.615312,
              "wallSeconds": 22.727161049000188,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H0/state.json"
            },
            {
              "seed": 43,
              "status": "completed",
              "cnnScore": 0.9603713155,
              "energy": -9.6676,
              "cnnAffinity": 5.8206458092,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 26.131853000000003,
              "wallSeconds": 16.445628138999837,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H0/state.json"
            }
          ]
        },
        "H1": {
          "completedRepeats": 2,
          "validRepeats": 2,
          "pairedValid": true,
          "cnnMedian": 0.9604794681,
          "energyMedian": -9.821745,
          "cnnRange": 0.00021630520000004427,
          "shortlisted": true,
          "rank": 4,
          "evidenceId": "H1-ev_tool_s2_gnina_batch_01_CHEMBL1207227",
          "source": "runs/lab_comparison_v2/H1/state.json",
          "jobs": [
            {
              "seed": 17,
              "status": "completed",
              "cnnScore": 0.9605876207,
              "energy": -9.97589,
              "cnnAffinity": 5.8956823349,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 26.631366999999997,
              "wallSeconds": 22.77492809799969,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H1/state.json"
            },
            {
              "seed": 43,
              "status": "completed",
              "cnnScore": 0.9603713155,
              "energy": -9.6676,
              "cnnAffinity": 5.8206458092,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 25.805984000000002,
              "wallSeconds": 16.197822584000278,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H1/state.json"
            }
          ]
        }
      }
    },
    {
      "id": "CHEMBL1241566",
      "smiles": "Cc1ccc(F)c(Nc2nccc(-c3ccc(S(N)(=O)=O)cc3)n2)c1",
      "ic50Nm": 32.00000000000001,
      "assayId": "CHEMBL1250281",
      "scaffold": "scaf_dbab6ba7f2c4",
      "activityIds": [
        3446965
      ],
      "documentIds": [
        "CHEMBL1240533"
      ],
      "results": {
        "baseline": {
          "completedRepeats": 2,
          "validRepeats": 2,
          "pairedValid": true,
          "cnnMedian": 0.9832958281,
          "energyMedian": -9.681905,
          "cnnRange": 0.003421366199999998,
          "shortlisted": true,
          "rank": 3,
          "evidenceId": "baseline-calculation-CHEMBL1241566",
          "source": "runs/lab_comparison_v2/fixed_baseline/state.json",
          "jobs": [
            {
              "seed": 17,
              "status": "completed",
              "cnnScore": 0.981585145,
              "energy": -9.66394,
              "cnnAffinity": 7.2644944191,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 34.631029,
              "wallSeconds": 27.54347714200003,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_002/jobs/CHEMBL1241566_s17/result.json"
            },
            {
              "seed": 43,
              "status": "completed",
              "cnnScore": 0.9850065112,
              "energy": -9.69987,
              "cnnAffinity": 7.3128728867,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 34.257844999999996,
              "wallSeconds": 21.40563407800005,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_002/jobs/CHEMBL1241566_s43/result.json"
            }
          ]
        },
        "H0": {
          "completedRepeats": 2,
          "validRepeats": 2,
          "pairedValid": true,
          "cnnMedian": 0.9832958281,
          "energyMedian": -9.681905,
          "cnnRange": 0.003421366199999998,
          "shortlisted": true,
          "rank": 3,
          "evidenceId": "H0-ev_tool_s4_gnina_01_CHEMBL1241566",
          "source": "runs/lab_comparison_v2/H0/state.json",
          "jobs": [
            {
              "seed": 17,
              "status": "completed",
              "cnnScore": 0.981585145,
              "energy": -9.66394,
              "cnnAffinity": 7.2644944191,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 34.72514,
              "wallSeconds": 27.641064534999714,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H0/state.json"
            },
            {
              "seed": 43,
              "status": "completed",
              "cnnScore": 0.9850065112,
              "energy": -9.69987,
              "cnnAffinity": 7.3128728867,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 34.711796,
              "wallSeconds": 21.52959762799992,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H0/state.json"
            }
          ]
        }
      }
    },
    {
      "id": "CHEMBL258805",
      "smiles": "COc1ccc2c(c1)/C(=C/c1cnc[nH]1)C(=O)N2",
      "ic50Nm": 35,
      "assayId": "CHEMBL1250281",
      "scaffold": "scaf_2c4cec1abe48",
      "activityIds": [
        3446953
      ],
      "documentIds": [
        "CHEMBL1240533"
      ],
      "results": {
        "H1": {
          "completedRepeats": 2,
          "validRepeats": 2,
          "pairedValid": true,
          "cnnMedian": 0.98735496405,
          "energyMedian": -7.967040000000001,
          "cnnRange": 0.0036378503000000784,
          "shortlisted": true,
          "rank": 3,
          "evidenceId": "H1-ev_tool_s4_gnina_batch_01_CHEMBL258805",
          "source": "runs/lab_comparison_v2/H1/state.json",
          "jobs": [
            {
              "seed": 17,
              "status": "completed",
              "cnnScore": 0.9891738892,
              "energy": -7.9884,
              "cnnAffinity": 5.9353756905,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 18.230591,
              "wallSeconds": 17.522970229999828,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H1/state.json"
            },
            {
              "seed": 43,
              "status": "completed",
              "cnnScore": 0.9855360389,
              "energy": -7.94568,
              "cnnAffinity": 5.8525061607,
              "poseValid": true,
              "identityPreserved": true,
              "failedChecks": [],
              "cpuSeconds": 17.193533,
              "wallSeconds": 11.376708262000193,
              "gpuObserved": true,
              "source": "runs/lab_comparison_v2/H1/state.json"
            }
          ]
        }
      }
    },
    {
      "id": "CHEMBL115220",
      "smiles": "O=C(Cc1ccc2ccccc2c1)Nc1cc(C2CC2)n[nH]1",
      "ic50Nm": 37,
      "assayId": "CHEMBL1250281",
      "scaffold": "scaf_091857d28cb1",
      "activityIds": [
        3446972
      ],
      "documentIds": [
        "CHEMBL1240533"
      ],
      "results": {}
    },
    {
      "id": "CHEMBL79049",
      "smiles": "CCCCOc1c(C(=O)c2c(F)cc(F)cc2F)c[nH]c2nncc1-2",
      "ic50Nm": 49.99999999999999,
      "assayId": "CHEMBL1250281",
      "scaffold": "scaf_2a9c510f0fd4",
      "activityIds": [
        3446952
      ],
      "documentIds": [
        "CHEMBL1240533"
      ],
      "results": {}
    },
    {
      "id": "CHEMBL336242",
      "smiles": "O=C(Nc1cc(CN[C@H]2Cc3ccc(Cl)cc3C2)n[nH]1)Nc1cccc2c1[C@@H]1CCCN1C2=O",
      "ic50Nm": 77.99999999999999,
      "assayId": "CHEMBL1250281",
      "scaffold": "scaf_df856ff73651",
      "activityIds": [
        3446956
      ],
      "documentIds": [
        "CHEMBL1240533"
      ],
      "results": {}
    },
    {
      "id": "CHEMBL141247",
      "smiles": "O=C(Nc1ccccn1)Nc1cccc2c1[C@H]1CCCN1C2=O",
      "ic50Nm": 440,
      "assayId": "CHEMBL1250281",
      "scaffold": "scaf_b86a540e03c6",
      "activityIds": [
        3446957
      ],
      "documentIds": [
        "CHEMBL1240533"
      ],
      "results": {}
    },
    {
      "id": "CHEMBL1242833",
      "smiles": "CC(C)(C)Nc1nc(C(F)(F)F)nc2ccc(Nc3ccccc3)cc12",
      "ic50Nm": 599.9999999999997,
      "assayId": "CHEMBL1250281",
      "scaffold": "scaf_c0d94e2dc18e",
      "activityIds": [
        3446961
      ],
      "documentIds": [
        "CHEMBL1240533"
      ],
      "results": {}
    },
    {
      "id": "CHEMBL14762",
      "smiles": "CC[C@H](CO)Nc1nc(NCc2ccccc2)c2ncn(C(C)C)c2n1",
      "ic50Nm": 700.0000000000001,
      "assayId": "CHEMBL1250281",
      "scaffold": "scaf_826021fc46ed",
      "activityIds": [
        3446949
      ],
      "documentIds": [
        "CHEMBL1240533"
      ],
      "results": {}
    },
    {
      "id": "CHEMBL16484",
      "smiles": "CC(C)(C)Nc1nc(C(F)(F)F)nc2ccc(-c3cccnc3)cc12",
      "ic50Nm": 1199.9999999999995,
      "assayId": "CHEMBL1250281",
      "scaffold": "scaf_3b3757d88141",
      "activityIds": [
        3446962
      ],
      "documentIds": [
        "CHEMBL1240533"
      ],
      "results": {}
    },
    {
      "id": "CHEMBL1242973",
      "smiles": "COC(=O)[C@@]1(Cc2ccc(O)c(CC=C(C)C)c2)OC(=O)C(O)=C1c1ccc(O)cc1",
      "ic50Nm": 1500.0000000000005,
      "assayId": "CHEMBL1250281",
      "scaffold": "scaf_541efe8edfc2",
      "activityIds": [
        3446958
      ],
      "documentIds": [
        "CHEMBL1240533"
      ],
      "results": {}
    },
    {
      "id": "CHEMBL133498",
      "smiles": "CN1CC[C@H](c2c(O)cc(O)c3c(=O)cc(Oc4ccccc4Cl)oc23)[C@H](O)C1",
      "ic50Nm": 2109.9999999999995,
      "assayId": "CHEMBL1250281",
      "scaffold": "scaf_e816bf2e57b7",
      "activityIds": [
        3446959
      ],
      "documentIds": [
        "CHEMBL1240533"
      ],
      "results": {}
    },
    {
      "id": "CHEMBL1241661",
      "smiles": "Nc1nc(Nc2ccc(S(N)(=O)=O)cc2)nc(OCc2ccccc2)c1N=O",
      "ic50Nm": 2199.9999999999995,
      "assayId": "CHEMBL1250281",
      "scaffold": "scaf_e3468222b6f8",
      "activityIds": [
        3446968
      ],
      "documentIds": [
        "CHEMBL1240533"
      ],
      "results": {}
    },
    {
      "id": "CHEMBL280074",
      "smiles": "Cn1cnc2c(NCc3ccccc3)nc(NCCO)nc21",
      "ic50Nm": 7000.000000000002,
      "assayId": "CHEMBL1250281",
      "scaffold": "scaf_826021fc46ed",
      "activityIds": [
        3446948
      ],
      "documentIds": [
        "CHEMBL1240533"
      ],
      "results": {}
    }
  ],
  "evidence": [
    {
      "id": "public-CHEMBL102926",
      "arm": "shared",
      "candidateId": "CHEMBL102926",
      "kind": "public_measurement",
      "title": "Public IC50 · CHEMBL102926",
      "source": "data/lab_cdk2_v2/candidates.json",
      "detail": "Historical ChEMBL assay measurement available to every arm; not a new experimental result or hidden evaluation label.",
      "record": {
        "assayId": "CHEMBL1250281",
        "ic50Nm": 1,
        "unit": "nM",
        "activityIds": [
          3446964
        ],
        "documentIds": [
          "CHEMBL1240533"
        ],
        "database": "ChEMBL",
        "databaseVersion": "ChEMBL_37"
      }
    },
    {
      "id": "public-CHEMBL194721",
      "arm": "shared",
      "candidateId": "CHEMBL194721",
      "kind": "public_measurement",
      "title": "Public IC50 · CHEMBL194721",
      "source": "data/lab_cdk2_v2/candidates.json",
      "detail": "Historical ChEMBL assay measurement available to every arm; not a new experimental result or hidden evaluation label.",
      "record": {
        "assayId": "CHEMBL1250281",
        "ic50Nm": 2,
        "unit": "nM",
        "activityIds": [
          3446973
        ],
        "documentIds": [
          "CHEMBL1240533"
        ],
        "database": "ChEMBL",
        "databaseVersion": "ChEMBL_37"
      }
    },
    {
      "id": "public-CHEMBL1241849",
      "arm": "shared",
      "candidateId": "CHEMBL1241849",
      "kind": "public_measurement",
      "title": "Public IC50 · CHEMBL1241849",
      "source": "data/lab_cdk2_v2/candidates.json",
      "detail": "Historical ChEMBL assay measurement available to every arm; not a new experimental result or hidden evaluation label.",
      "record": {
        "assayId": "CHEMBL1250281",
        "ic50Nm": 5.000000000000001,
        "unit": "nM",
        "activityIds": [
          3446971
        ],
        "documentIds": [
          "CHEMBL1240533"
        ],
        "database": "ChEMBL",
        "databaseVersion": "ChEMBL_37"
      }
    },
    {
      "id": "public-CHEMBL1241567",
      "arm": "shared",
      "candidateId": "CHEMBL1241567",
      "kind": "public_measurement",
      "title": "Public IC50 · CHEMBL1241567",
      "source": "data/lab_cdk2_v2/candidates.json",
      "detail": "Historical ChEMBL assay measurement available to every arm; not a new experimental result or hidden evaluation label.",
      "record": {
        "assayId": "CHEMBL1250281",
        "ic50Nm": 6,
        "unit": "nM",
        "activityIds": [
          3446966
        ],
        "documentIds": [
          "CHEMBL1240533"
        ],
        "database": "ChEMBL",
        "databaseVersion": "ChEMBL_37"
      }
    },
    {
      "id": "public-CHEMBL362560",
      "arm": "shared",
      "candidateId": "CHEMBL362560",
      "kind": "public_measurement",
      "title": "Public IC50 · CHEMBL362560",
      "source": "data/lab_cdk2_v2/candidates.json",
      "detail": "Historical ChEMBL assay measurement available to every arm; not a new experimental result or hidden evaluation label.",
      "record": {
        "assayId": "CHEMBL1250281",
        "ic50Nm": 9,
        "unit": "nM",
        "activityIds": [
          3446970
        ],
        "documentIds": [
          "CHEMBL1240533"
        ],
        "database": "ChEMBL",
        "databaseVersion": "ChEMBL_37"
      }
    },
    {
      "id": "public-CHEMBL50894",
      "arm": "shared",
      "candidateId": "CHEMBL50894",
      "kind": "public_measurement",
      "title": "Public IC50 · CHEMBL50894",
      "source": "data/lab_cdk2_v2/candidates.json",
      "detail": "Historical ChEMBL assay measurement available to every arm; not a new experimental result or hidden evaluation label.",
      "record": {
        "assayId": "CHEMBL1250281",
        "ic50Nm": 15.000000000000004,
        "unit": "nM",
        "activityIds": [
          3446955
        ],
        "documentIds": [
          "CHEMBL1240533"
        ],
        "database": "ChEMBL",
        "databaseVersion": "ChEMBL_37"
      }
    },
    {
      "id": "public-CHEMBL1207227",
      "arm": "shared",
      "candidateId": "CHEMBL1207227",
      "kind": "public_measurement",
      "title": "Public IC50 · CHEMBL1207227",
      "source": "data/lab_cdk2_v2/candidates.json",
      "detail": "Historical ChEMBL assay measurement available to every arm; not a new experimental result or hidden evaluation label.",
      "record": {
        "assayId": "CHEMBL1250281",
        "ic50Nm": 21.999999999999996,
        "unit": "nM",
        "activityIds": [
          3446954
        ],
        "documentIds": [
          "CHEMBL1240533"
        ],
        "database": "ChEMBL",
        "databaseVersion": "ChEMBL_37"
      }
    },
    {
      "id": "public-CHEMBL1241566",
      "arm": "shared",
      "candidateId": "CHEMBL1241566",
      "kind": "public_measurement",
      "title": "Public IC50 · CHEMBL1241566",
      "source": "data/lab_cdk2_v2/candidates.json",
      "detail": "Historical ChEMBL assay measurement available to every arm; not a new experimental result or hidden evaluation label.",
      "record": {
        "assayId": "CHEMBL1250281",
        "ic50Nm": 32.00000000000001,
        "unit": "nM",
        "activityIds": [
          3446965
        ],
        "documentIds": [
          "CHEMBL1240533"
        ],
        "database": "ChEMBL",
        "databaseVersion": "ChEMBL_37"
      }
    },
    {
      "id": "public-CHEMBL258805",
      "arm": "shared",
      "candidateId": "CHEMBL258805",
      "kind": "public_measurement",
      "title": "Public IC50 · CHEMBL258805",
      "source": "data/lab_cdk2_v2/candidates.json",
      "detail": "Historical ChEMBL assay measurement available to every arm; not a new experimental result or hidden evaluation label.",
      "record": {
        "assayId": "CHEMBL1250281",
        "ic50Nm": 35,
        "unit": "nM",
        "activityIds": [
          3446953
        ],
        "documentIds": [
          "CHEMBL1240533"
        ],
        "database": "ChEMBL",
        "databaseVersion": "ChEMBL_37"
      }
    },
    {
      "id": "public-CHEMBL115220",
      "arm": "shared",
      "candidateId": "CHEMBL115220",
      "kind": "public_measurement",
      "title": "Public IC50 · CHEMBL115220",
      "source": "data/lab_cdk2_v2/candidates.json",
      "detail": "Historical ChEMBL assay measurement available to every arm; not a new experimental result or hidden evaluation label.",
      "record": {
        "assayId": "CHEMBL1250281",
        "ic50Nm": 37,
        "unit": "nM",
        "activityIds": [
          3446972
        ],
        "documentIds": [
          "CHEMBL1240533"
        ],
        "database": "ChEMBL",
        "databaseVersion": "ChEMBL_37"
      }
    },
    {
      "id": "public-CHEMBL79049",
      "arm": "shared",
      "candidateId": "CHEMBL79049",
      "kind": "public_measurement",
      "title": "Public IC50 · CHEMBL79049",
      "source": "data/lab_cdk2_v2/candidates.json",
      "detail": "Historical ChEMBL assay measurement available to every arm; not a new experimental result or hidden evaluation label.",
      "record": {
        "assayId": "CHEMBL1250281",
        "ic50Nm": 49.99999999999999,
        "unit": "nM",
        "activityIds": [
          3446952
        ],
        "documentIds": [
          "CHEMBL1240533"
        ],
        "database": "ChEMBL",
        "databaseVersion": "ChEMBL_37"
      }
    },
    {
      "id": "public-CHEMBL336242",
      "arm": "shared",
      "candidateId": "CHEMBL336242",
      "kind": "public_measurement",
      "title": "Public IC50 · CHEMBL336242",
      "source": "data/lab_cdk2_v2/candidates.json",
      "detail": "Historical ChEMBL assay measurement available to every arm; not a new experimental result or hidden evaluation label.",
      "record": {
        "assayId": "CHEMBL1250281",
        "ic50Nm": 77.99999999999999,
        "unit": "nM",
        "activityIds": [
          3446956
        ],
        "documentIds": [
          "CHEMBL1240533"
        ],
        "database": "ChEMBL",
        "databaseVersion": "ChEMBL_37"
      }
    },
    {
      "id": "public-CHEMBL141247",
      "arm": "shared",
      "candidateId": "CHEMBL141247",
      "kind": "public_measurement",
      "title": "Public IC50 · CHEMBL141247",
      "source": "data/lab_cdk2_v2/candidates.json",
      "detail": "Historical ChEMBL assay measurement available to every arm; not a new experimental result or hidden evaluation label.",
      "record": {
        "assayId": "CHEMBL1250281",
        "ic50Nm": 440,
        "unit": "nM",
        "activityIds": [
          3446957
        ],
        "documentIds": [
          "CHEMBL1240533"
        ],
        "database": "ChEMBL",
        "databaseVersion": "ChEMBL_37"
      }
    },
    {
      "id": "public-CHEMBL1242833",
      "arm": "shared",
      "candidateId": "CHEMBL1242833",
      "kind": "public_measurement",
      "title": "Public IC50 · CHEMBL1242833",
      "source": "data/lab_cdk2_v2/candidates.json",
      "detail": "Historical ChEMBL assay measurement available to every arm; not a new experimental result or hidden evaluation label.",
      "record": {
        "assayId": "CHEMBL1250281",
        "ic50Nm": 599.9999999999997,
        "unit": "nM",
        "activityIds": [
          3446961
        ],
        "documentIds": [
          "CHEMBL1240533"
        ],
        "database": "ChEMBL",
        "databaseVersion": "ChEMBL_37"
      }
    },
    {
      "id": "public-CHEMBL14762",
      "arm": "shared",
      "candidateId": "CHEMBL14762",
      "kind": "public_measurement",
      "title": "Public IC50 · CHEMBL14762",
      "source": "data/lab_cdk2_v2/candidates.json",
      "detail": "Historical ChEMBL assay measurement available to every arm; not a new experimental result or hidden evaluation label.",
      "record": {
        "assayId": "CHEMBL1250281",
        "ic50Nm": 700.0000000000001,
        "unit": "nM",
        "activityIds": [
          3446949
        ],
        "documentIds": [
          "CHEMBL1240533"
        ],
        "database": "ChEMBL",
        "databaseVersion": "ChEMBL_37"
      }
    },
    {
      "id": "public-CHEMBL16484",
      "arm": "shared",
      "candidateId": "CHEMBL16484",
      "kind": "public_measurement",
      "title": "Public IC50 · CHEMBL16484",
      "source": "data/lab_cdk2_v2/candidates.json",
      "detail": "Historical ChEMBL assay measurement available to every arm; not a new experimental result or hidden evaluation label.",
      "record": {
        "assayId": "CHEMBL1250281",
        "ic50Nm": 1199.9999999999995,
        "unit": "nM",
        "activityIds": [
          3446962
        ],
        "documentIds": [
          "CHEMBL1240533"
        ],
        "database": "ChEMBL",
        "databaseVersion": "ChEMBL_37"
      }
    },
    {
      "id": "public-CHEMBL1242973",
      "arm": "shared",
      "candidateId": "CHEMBL1242973",
      "kind": "public_measurement",
      "title": "Public IC50 · CHEMBL1242973",
      "source": "data/lab_cdk2_v2/candidates.json",
      "detail": "Historical ChEMBL assay measurement available to every arm; not a new experimental result or hidden evaluation label.",
      "record": {
        "assayId": "CHEMBL1250281",
        "ic50Nm": 1500.0000000000005,
        "unit": "nM",
        "activityIds": [
          3446958
        ],
        "documentIds": [
          "CHEMBL1240533"
        ],
        "database": "ChEMBL",
        "databaseVersion": "ChEMBL_37"
      }
    },
    {
      "id": "public-CHEMBL133498",
      "arm": "shared",
      "candidateId": "CHEMBL133498",
      "kind": "public_measurement",
      "title": "Public IC50 · CHEMBL133498",
      "source": "data/lab_cdk2_v2/candidates.json",
      "detail": "Historical ChEMBL assay measurement available to every arm; not a new experimental result or hidden evaluation label.",
      "record": {
        "assayId": "CHEMBL1250281",
        "ic50Nm": 2109.9999999999995,
        "unit": "nM",
        "activityIds": [
          3446959
        ],
        "documentIds": [
          "CHEMBL1240533"
        ],
        "database": "ChEMBL",
        "databaseVersion": "ChEMBL_37"
      }
    },
    {
      "id": "public-CHEMBL1241661",
      "arm": "shared",
      "candidateId": "CHEMBL1241661",
      "kind": "public_measurement",
      "title": "Public IC50 · CHEMBL1241661",
      "source": "data/lab_cdk2_v2/candidates.json",
      "detail": "Historical ChEMBL assay measurement available to every arm; not a new experimental result or hidden evaluation label.",
      "record": {
        "assayId": "CHEMBL1250281",
        "ic50Nm": 2199.9999999999995,
        "unit": "nM",
        "activityIds": [
          3446968
        ],
        "documentIds": [
          "CHEMBL1240533"
        ],
        "database": "ChEMBL",
        "databaseVersion": "ChEMBL_37"
      }
    },
    {
      "id": "public-CHEMBL280074",
      "arm": "shared",
      "candidateId": "CHEMBL280074",
      "kind": "public_measurement",
      "title": "Public IC50 · CHEMBL280074",
      "source": "data/lab_cdk2_v2/candidates.json",
      "detail": "Historical ChEMBL assay measurement available to every arm; not a new experimental result or hidden evaluation label.",
      "record": {
        "assayId": "CHEMBL1250281",
        "ic50Nm": 7000.000000000002,
        "unit": "nM",
        "activityIds": [
          3446948
        ],
        "documentIds": [
          "CHEMBL1240533"
        ],
        "database": "ChEMBL",
        "databaseVersion": "ChEMBL_37"
      }
    },
    {
      "id": "baseline-calculation-CHEMBL102926",
      "arm": "baseline",
      "candidateId": "CHEMBL102926",
      "kind": "actual_computational_results",
      "title": "GNINA paired calculation · CHEMBL102926",
      "source": "runs/lab_comparison_v2/fixed_baseline/state.json",
      "detail": "Recorded GNINA calculations with pose-validity and identity checks. CNNscore is a computational ranking signal, not biological activity. Qualified medians are absent when the paired-valid requirement is not met.",
      "record": {
        "protocolId": "lab_cdk2_gnina_p0profile_comparison_v2",
        "protocolHash": "69f1ee145a3bbced30d0e717d35dee296c4d2b0c823ce04f3bb848e9ae8637e0",
        "completedRepeats": 2,
        "validRepeats": 0,
        "pairedValid": false,
        "rawCnnMedian": 0.9511046409499999,
        "qualifiedCnnMedian": null,
        "rawEnergyMedian": -8.416184999999999,
        "qualifiedEnergyMedian": null,
        "energyUnit": "kcal/mol",
        "cnnRange": 0.04895198349999996,
        "officialAffinityPass": null,
        "jobs": [
          {
            "seed": 17,
            "status": "completed",
            "cnnScore": 0.9266286492,
            "energy": -8.67488,
            "cnnAffinity": 7.9868850708,
            "poseValid": false,
            "identityPreserved": false,
            "failedChecks": [],
            "cpuSeconds": 67.656649,
            "wallSeconds": 47.05838598999992,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_001/jobs/CHEMBL102926_s17/result.json"
          },
          {
            "seed": 43,
            "status": "completed",
            "cnnScore": 0.9755806327,
            "energy": -8.15749,
            "cnnAffinity": 7.8927283287,
            "poseValid": false,
            "identityPreserved": false,
            "failedChecks": [],
            "cpuSeconds": 66.721529,
            "wallSeconds": 46.43906837899999,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_001/jobs/CHEMBL102926_s43/result.json"
          }
        ]
      }
    },
    {
      "id": "baseline-calculation-CHEMBL194721",
      "arm": "baseline",
      "candidateId": "CHEMBL194721",
      "kind": "actual_computational_results",
      "title": "GNINA paired calculation · CHEMBL194721",
      "source": "runs/lab_comparison_v2/fixed_baseline/state.json",
      "detail": "Recorded GNINA calculations with pose-validity and identity checks. CNNscore is a computational ranking signal, not biological activity. Qualified medians are absent when the paired-valid requirement is not met.",
      "record": {
        "protocolId": "lab_cdk2_gnina_p0profile_comparison_v2",
        "protocolHash": "69f1ee145a3bbced30d0e717d35dee296c4d2b0c823ce04f3bb848e9ae8637e0",
        "completedRepeats": 2,
        "validRepeats": 2,
        "pairedValid": true,
        "rawCnnMedian": 0.99269136785,
        "qualifiedCnnMedian": 0.99269136785,
        "rawEnergyMedian": -9.114985,
        "qualifiedEnergyMedian": -9.114985,
        "energyUnit": "kcal/mol",
        "cnnRange": 0.004059851100000067,
        "officialAffinityPass": null,
        "jobs": [
          {
            "seed": 17,
            "status": "completed",
            "cnnScore": 0.9947212934,
            "energy": -9.30373,
            "cnnAffinity": 7.18116045,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 45.769684000000005,
            "wallSeconds": 32.99960110699999,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_001/jobs/CHEMBL194721_s17/result.json"
          },
          {
            "seed": 43,
            "status": "completed",
            "cnnScore": 0.9906614423,
            "energy": -8.92624,
            "cnnAffinity": 7.2469830513,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 46.990868,
            "wallSeconds": 32.180950518000145,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_001/jobs/CHEMBL194721_s43/result.json"
          }
        ]
      }
    },
    {
      "id": "baseline-calculation-CHEMBL1241849",
      "arm": "baseline",
      "candidateId": "CHEMBL1241849",
      "kind": "actual_computational_results",
      "title": "GNINA paired calculation · CHEMBL1241849",
      "source": "runs/lab_comparison_v2/fixed_baseline/state.json",
      "detail": "Recorded GNINA calculations with pose-validity and identity checks. CNNscore is a computational ranking signal, not biological activity. Qualified medians are absent when the paired-valid requirement is not met.",
      "record": {
        "protocolId": "lab_cdk2_gnina_p0profile_comparison_v2",
        "protocolHash": "69f1ee145a3bbced30d0e717d35dee296c4d2b0c823ce04f3bb848e9ae8637e0",
        "completedRepeats": 2,
        "validRepeats": 2,
        "pairedValid": true,
        "rawCnnMedian": 0.64205130935,
        "qualifiedCnnMedian": 0.64205130935,
        "rawEnergyMedian": -9.301580000000001,
        "qualifiedEnergyMedian": -9.301580000000001,
        "energyUnit": "kcal/mol",
        "cnnRange": 0.10435503729999995,
        "officialAffinityPass": null,
        "jobs": [
          {
            "seed": 17,
            "status": "completed",
            "cnnScore": 0.694228828,
            "energy": -9.08103,
            "cnnAffinity": 6.8536119461,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 69.49141,
            "wallSeconds": 45.783677876999946,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_001/jobs/CHEMBL1241849_s17/result.json"
          },
          {
            "seed": 43,
            "status": "completed",
            "cnnScore": 0.5898737907,
            "energy": -9.52213,
            "cnnAffinity": 6.9678077698,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 67.707283,
            "wallSeconds": 46.244579925999915,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_001/jobs/CHEMBL1241849_s43/result.json"
          }
        ]
      }
    },
    {
      "id": "baseline-calculation-CHEMBL1241567",
      "arm": "baseline",
      "candidateId": "CHEMBL1241567",
      "kind": "actual_computational_results",
      "title": "GNINA paired calculation · CHEMBL1241567",
      "source": "runs/lab_comparison_v2/fixed_baseline/state.json",
      "detail": "Recorded GNINA calculations with pose-validity and identity checks. CNNscore is a computational ranking signal, not biological activity. Qualified medians are absent when the paired-valid requirement is not met.",
      "record": {
        "protocolId": "lab_cdk2_gnina_p0profile_comparison_v2",
        "protocolHash": "69f1ee145a3bbced30d0e717d35dee296c4d2b0c823ce04f3bb848e9ae8637e0",
        "completedRepeats": 2,
        "validRepeats": 2,
        "pairedValid": true,
        "rawCnnMedian": 0.91990068555,
        "qualifiedCnnMedian": 0.91990068555,
        "rawEnergyMedian": -7.829445,
        "qualifiedEnergyMedian": -7.829445,
        "energyUnit": "kcal/mol",
        "cnnRange": 0.027980029500000003,
        "officialAffinityPass": null,
        "jobs": [
          {
            "seed": 17,
            "status": "completed",
            "cnnScore": 0.9338907003,
            "energy": -7.31572,
            "cnnAffinity": 7.0732722282,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 44.452085,
            "wallSeconds": 27.8476080800001,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_001/jobs/CHEMBL1241567_s17/result.json"
          },
          {
            "seed": 43,
            "status": "completed",
            "cnnScore": 0.9059106708,
            "energy": -8.34317,
            "cnnAffinity": 7.0896420479,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 43.208194,
            "wallSeconds": 27.5672064649998,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_001/jobs/CHEMBL1241567_s43/result.json"
          }
        ]
      }
    },
    {
      "id": "baseline-calculation-CHEMBL362560",
      "arm": "baseline",
      "candidateId": "CHEMBL362560",
      "kind": "actual_computational_results",
      "title": "GNINA paired calculation · CHEMBL362560",
      "source": "runs/lab_comparison_v2/fixed_baseline/state.json",
      "detail": "Recorded GNINA calculations with pose-validity and identity checks. CNNscore is a computational ranking signal, not biological activity. Qualified medians are absent when the paired-valid requirement is not met.",
      "record": {
        "protocolId": "lab_cdk2_gnina_p0profile_comparison_v2",
        "protocolHash": "69f1ee145a3bbced30d0e717d35dee296c4d2b0c823ce04f3bb848e9ae8637e0",
        "completedRepeats": 2,
        "validRepeats": 1,
        "pairedValid": false,
        "rawCnnMedian": 0.47194273769999995,
        "qualifiedCnnMedian": null,
        "rawEnergyMedian": -11.558254999999999,
        "qualifiedEnergyMedian": null,
        "energyUnit": "kcal/mol",
        "cnnRange": 0.3979472816,
        "officialAffinityPass": null,
        "jobs": [
          {
            "seed": 17,
            "status": "completed",
            "cnnScore": 0.2729690969,
            "energy": -10.93711,
            "cnnAffinity": 6.9387550354,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 67.31528200000001,
            "wallSeconds": 45.643975573000034,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_002/jobs/CHEMBL362560_s17/result.json"
          },
          {
            "seed": 43,
            "status": "completed",
            "cnnScore": 0.6709163785,
            "energy": -12.1794,
            "cnnAffinity": 7.205634594,
            "poseValid": false,
            "identityPreserved": true,
            "failedChecks": [
              "internal_steric_clash",
              "internal_energy"
            ],
            "cpuSeconds": 70.039497,
            "wallSeconds": 45.41462274100013,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_002/jobs/CHEMBL362560_s43/result.json"
          }
        ]
      }
    },
    {
      "id": "baseline-calculation-CHEMBL50894",
      "arm": "baseline",
      "candidateId": "CHEMBL50894",
      "kind": "actual_computational_results",
      "title": "GNINA paired calculation · CHEMBL50894",
      "source": "runs/lab_comparison_v2/fixed_baseline/state.json",
      "detail": "Recorded GNINA calculations with pose-validity and identity checks. CNNscore is a computational ranking signal, not biological activity. Qualified medians are absent when the paired-valid requirement is not met.",
      "record": {
        "protocolId": "lab_cdk2_gnina_p0profile_comparison_v2",
        "protocolHash": "69f1ee145a3bbced30d0e717d35dee296c4d2b0c823ce04f3bb848e9ae8637e0",
        "completedRepeats": 2,
        "validRepeats": 2,
        "pairedValid": true,
        "rawCnnMedian": 0.99664747715,
        "qualifiedCnnMedian": 0.99664747715,
        "rawEnergyMedian": -9.98197,
        "qualifiedEnergyMedian": -9.98197,
        "energyUnit": "kcal/mol",
        "cnnRange": 0.000005125900000013672,
        "officialAffinityPass": null,
        "jobs": [
          {
            "seed": 17,
            "status": "completed",
            "cnnScore": 0.9966449142,
            "energy": -9.97848,
            "cnnAffinity": 6.4814443588,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 16.60511,
            "wallSeconds": 11.324848820999932,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_002/jobs/CHEMBL50894_s17/result.json"
          },
          {
            "seed": 43,
            "status": "completed",
            "cnnScore": 0.9966500401,
            "energy": -9.98546,
            "cnnAffinity": 6.4787092209,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 17.577929,
            "wallSeconds": 11.93734404299994,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_002/jobs/CHEMBL50894_s43/result.json"
          }
        ]
      }
    },
    {
      "id": "baseline-calculation-CHEMBL1207227",
      "arm": "baseline",
      "candidateId": "CHEMBL1207227",
      "kind": "actual_computational_results",
      "title": "GNINA paired calculation · CHEMBL1207227",
      "source": "runs/lab_comparison_v2/fixed_baseline/state.json",
      "detail": "Recorded GNINA calculations with pose-validity and identity checks. CNNscore is a computational ranking signal, not biological activity. Qualified medians are absent when the paired-valid requirement is not met.",
      "record": {
        "protocolId": "lab_cdk2_gnina_p0profile_comparison_v2",
        "protocolHash": "69f1ee145a3bbced30d0e717d35dee296c4d2b0c823ce04f3bb848e9ae8637e0",
        "completedRepeats": 2,
        "validRepeats": 2,
        "pairedValid": true,
        "rawCnnMedian": 0.9604794681,
        "qualifiedCnnMedian": 0.9604794681,
        "rawEnergyMedian": -9.821745,
        "qualifiedEnergyMedian": -9.821745,
        "energyUnit": "kcal/mol",
        "cnnRange": 0.00021630520000004427,
        "officialAffinityPass": null,
        "jobs": [
          {
            "seed": 17,
            "status": "completed",
            "cnnScore": 0.9605876207,
            "energy": -9.97589,
            "cnnAffinity": 5.8956823349,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 27.257942,
            "wallSeconds": 22.505802640000184,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_002/jobs/CHEMBL1207227_s17/result.json"
          },
          {
            "seed": 43,
            "status": "completed",
            "cnnScore": 0.9603713155,
            "energy": -9.6676,
            "cnnAffinity": 5.8206458092,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 25.987678,
            "wallSeconds": 16.341637284999933,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_002/jobs/CHEMBL1207227_s43/result.json"
          }
        ]
      }
    },
    {
      "id": "baseline-calculation-CHEMBL1241566",
      "arm": "baseline",
      "candidateId": "CHEMBL1241566",
      "kind": "actual_computational_results",
      "title": "GNINA paired calculation · CHEMBL1241566",
      "source": "runs/lab_comparison_v2/fixed_baseline/state.json",
      "detail": "Recorded GNINA calculations with pose-validity and identity checks. CNNscore is a computational ranking signal, not biological activity. Qualified medians are absent when the paired-valid requirement is not met.",
      "record": {
        "protocolId": "lab_cdk2_gnina_p0profile_comparison_v2",
        "protocolHash": "69f1ee145a3bbced30d0e717d35dee296c4d2b0c823ce04f3bb848e9ae8637e0",
        "completedRepeats": 2,
        "validRepeats": 2,
        "pairedValid": true,
        "rawCnnMedian": 0.9832958281,
        "qualifiedCnnMedian": 0.9832958281,
        "rawEnergyMedian": -9.681905,
        "qualifiedEnergyMedian": -9.681905,
        "energyUnit": "kcal/mol",
        "cnnRange": 0.003421366199999998,
        "officialAffinityPass": null,
        "jobs": [
          {
            "seed": 17,
            "status": "completed",
            "cnnScore": 0.981585145,
            "energy": -9.66394,
            "cnnAffinity": 7.2644944191,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 34.631029,
            "wallSeconds": 27.54347714200003,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_002/jobs/CHEMBL1241566_s17/result.json"
          },
          {
            "seed": 43,
            "status": "completed",
            "cnnScore": 0.9850065112,
            "energy": -9.69987,
            "cnnAffinity": 7.3128728867,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 34.257844999999996,
            "wallSeconds": 21.40563407800005,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_002/jobs/CHEMBL1241566_s43/result.json"
          }
        ]
      }
    },
    {
      "id": "H0-ev_tool_s0_gnina_01_CHEMBL102926",
      "arm": "H0",
      "candidateId": "CHEMBL102926",
      "kind": "actual_computational_results",
      "title": "GNINA paired calculation · CHEMBL102926",
      "source": "runs/lab_comparison_v2/H0/state.json",
      "detail": "Recorded GNINA calculations with pose-validity and identity checks. CNNscore is a computational ranking signal, not biological activity. Qualified medians are absent when the paired-valid requirement is not met.",
      "record": {
        "protocolId": "lab_cdk2_gnina_p0profile_comparison_v2",
        "protocolHash": "69f1ee145a3bbced30d0e717d35dee296c4d2b0c823ce04f3bb848e9ae8637e0",
        "completedRepeats": 2,
        "validRepeats": 0,
        "pairedValid": false,
        "rawCnnMedian": 0.9511046409499999,
        "qualifiedCnnMedian": null,
        "rawEnergyMedian": -8.416184999999999,
        "qualifiedEnergyMedian": null,
        "energyUnit": "kcal/mol",
        "cnnRange": 0.04895198349999996,
        "officialAffinityPass": null,
        "jobs": [
          {
            "seed": 17,
            "status": "completed",
            "cnnScore": 0.9266286492,
            "energy": -8.67488,
            "cnnAffinity": 7.9868850708,
            "poseValid": false,
            "identityPreserved": false,
            "failedChecks": [],
            "cpuSeconds": 72.64861499999999,
            "wallSeconds": 50.041647323999996,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H0/state.json"
          },
          {
            "seed": 43,
            "status": "completed",
            "cnnScore": 0.9755806327,
            "energy": -8.15749,
            "cnnAffinity": 7.8927283287,
            "poseValid": false,
            "identityPreserved": false,
            "failedChecks": [],
            "cpuSeconds": 70.65006500000001,
            "wallSeconds": 48.87615139900004,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H0/state.json"
          }
        ]
      }
    },
    {
      "id": "H0-ev_tool_s0_gnina_01_CHEMBL194721",
      "arm": "H0",
      "candidateId": "CHEMBL194721",
      "kind": "actual_computational_results",
      "title": "GNINA paired calculation · CHEMBL194721",
      "source": "runs/lab_comparison_v2/H0/state.json",
      "detail": "Recorded GNINA calculations with pose-validity and identity checks. CNNscore is a computational ranking signal, not biological activity. Qualified medians are absent when the paired-valid requirement is not met.",
      "record": {
        "protocolId": "lab_cdk2_gnina_p0profile_comparison_v2",
        "protocolHash": "69f1ee145a3bbced30d0e717d35dee296c4d2b0c823ce04f3bb848e9ae8637e0",
        "completedRepeats": 2,
        "validRepeats": 2,
        "pairedValid": true,
        "rawCnnMedian": 0.99269136785,
        "qualifiedCnnMedian": 0.99269136785,
        "rawEnergyMedian": -9.114985,
        "qualifiedEnergyMedian": -9.114985,
        "energyUnit": "kcal/mol",
        "cnnRange": 0.004059851100000067,
        "officialAffinityPass": null,
        "jobs": [
          {
            "seed": 17,
            "status": "completed",
            "cnnScore": 0.9947212934,
            "energy": -9.30373,
            "cnnAffinity": 7.18116045,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 46.522176,
            "wallSeconds": 33.91211764800005,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H0/state.json"
          },
          {
            "seed": 43,
            "status": "completed",
            "cnnScore": 0.9906614423,
            "energy": -8.92624,
            "cnnAffinity": 7.2469830513,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 48.256814,
            "wallSeconds": 30.04845338600012,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H0/state.json"
          }
        ]
      }
    },
    {
      "id": "H0-ev_tool_s0_gnina_01_CHEMBL1241567",
      "arm": "H0",
      "candidateId": "CHEMBL1241567",
      "kind": "actual_computational_results",
      "title": "GNINA paired calculation · CHEMBL1241567",
      "source": "runs/lab_comparison_v2/H0/state.json",
      "detail": "Recorded GNINA calculations with pose-validity and identity checks. CNNscore is a computational ranking signal, not biological activity. Qualified medians are absent when the paired-valid requirement is not met.",
      "record": {
        "protocolId": "lab_cdk2_gnina_p0profile_comparison_v2",
        "protocolHash": "69f1ee145a3bbced30d0e717d35dee296c4d2b0c823ce04f3bb848e9ae8637e0",
        "completedRepeats": 2,
        "validRepeats": 2,
        "pairedValid": true,
        "rawCnnMedian": 0.91990068555,
        "qualifiedCnnMedian": 0.91990068555,
        "rawEnergyMedian": -7.829445,
        "qualifiedEnergyMedian": -7.829445,
        "energyUnit": "kcal/mol",
        "cnnRange": 0.027980029500000003,
        "officialAffinityPass": null,
        "jobs": [
          {
            "seed": 17,
            "status": "completed",
            "cnnScore": 0.9338907003,
            "energy": -7.31572,
            "cnnAffinity": 7.0732722282,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 44.938658999999994,
            "wallSeconds": 28.208291645999907,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H0/state.json"
          },
          {
            "seed": 43,
            "status": "completed",
            "cnnScore": 0.9059106708,
            "energy": -8.34317,
            "cnnAffinity": 7.0896420479,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 44.378932,
            "wallSeconds": 33.17753011800005,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H0/state.json"
          }
        ]
      }
    },
    {
      "id": "H0-ev_tool_s0_gnina_01_CHEMBL362560",
      "arm": "H0",
      "candidateId": "CHEMBL362560",
      "kind": "actual_computational_results",
      "title": "GNINA paired calculation · CHEMBL362560",
      "source": "runs/lab_comparison_v2/H0/state.json",
      "detail": "Recorded GNINA calculations with pose-validity and identity checks. CNNscore is a computational ranking signal, not biological activity. Qualified medians are absent when the paired-valid requirement is not met.",
      "record": {
        "protocolId": "lab_cdk2_gnina_p0profile_comparison_v2",
        "protocolHash": "69f1ee145a3bbced30d0e717d35dee296c4d2b0c823ce04f3bb848e9ae8637e0",
        "completedRepeats": 2,
        "validRepeats": 1,
        "pairedValid": false,
        "rawCnnMedian": 0.47194273769999995,
        "qualifiedCnnMedian": null,
        "rawEnergyMedian": -11.558254999999999,
        "qualifiedEnergyMedian": null,
        "energyUnit": "kcal/mol",
        "cnnRange": 0.3979472816,
        "officialAffinityPass": null,
        "jobs": [
          {
            "seed": 17,
            "status": "completed",
            "cnnScore": 0.2729690969,
            "energy": -10.93711,
            "cnnAffinity": 6.9387550354,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 68.189663,
            "wallSeconds": 47.00404958599984,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H0/state.json"
          },
          {
            "seed": 43,
            "status": "completed",
            "cnnScore": 0.6709163785,
            "energy": -12.1794,
            "cnnAffinity": 7.205634594,
            "poseValid": false,
            "identityPreserved": true,
            "failedChecks": [
              "internal_steric_clash",
              "internal_energy"
            ],
            "cpuSeconds": 70.74819,
            "wallSeconds": 48.395093231000146,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H0/state.json"
          }
        ]
      }
    },
    {
      "id": "H0-ev_tool_s2_gnina_01_CHEMBL1241849",
      "arm": "H0",
      "candidateId": "CHEMBL1241849",
      "kind": "actual_computational_results",
      "title": "GNINA paired calculation · CHEMBL1241849",
      "source": "runs/lab_comparison_v2/H0/state.json",
      "detail": "Recorded GNINA calculations with pose-validity and identity checks. CNNscore is a computational ranking signal, not biological activity. Qualified medians are absent when the paired-valid requirement is not met.",
      "record": {
        "protocolId": "lab_cdk2_gnina_p0profile_comparison_v2",
        "protocolHash": "69f1ee145a3bbced30d0e717d35dee296c4d2b0c823ce04f3bb848e9ae8637e0",
        "completedRepeats": 2,
        "validRepeats": 2,
        "pairedValid": true,
        "rawCnnMedian": 0.64205130935,
        "qualifiedCnnMedian": 0.64205130935,
        "rawEnergyMedian": -9.301580000000001,
        "qualifiedEnergyMedian": -9.301580000000001,
        "energyUnit": "kcal/mol",
        "cnnRange": 0.10435503729999995,
        "officialAffinityPass": null,
        "jobs": [
          {
            "seed": 17,
            "status": "completed",
            "cnnScore": 0.694228828,
            "energy": -9.08103,
            "cnnAffinity": 6.8536119461,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 69.237651,
            "wallSeconds": 47.73175196400007,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H0/state.json"
          },
          {
            "seed": 43,
            "status": "completed",
            "cnnScore": 0.5898737907,
            "energy": -9.52213,
            "cnnAffinity": 6.9678077698,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 70.060393,
            "wallSeconds": 48.42985849099978,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H0/state.json"
          }
        ]
      }
    },
    {
      "id": "H0-ev_tool_s2_gnina_01_CHEMBL50894",
      "arm": "H0",
      "candidateId": "CHEMBL50894",
      "kind": "actual_computational_results",
      "title": "GNINA paired calculation · CHEMBL50894",
      "source": "runs/lab_comparison_v2/H0/state.json",
      "detail": "Recorded GNINA calculations with pose-validity and identity checks. CNNscore is a computational ranking signal, not biological activity. Qualified medians are absent when the paired-valid requirement is not met.",
      "record": {
        "protocolId": "lab_cdk2_gnina_p0profile_comparison_v2",
        "protocolHash": "69f1ee145a3bbced30d0e717d35dee296c4d2b0c823ce04f3bb848e9ae8637e0",
        "completedRepeats": 2,
        "validRepeats": 2,
        "pairedValid": true,
        "rawCnnMedian": 0.99664747715,
        "qualifiedCnnMedian": 0.99664747715,
        "rawEnergyMedian": -9.98197,
        "qualifiedEnergyMedian": -9.98197,
        "energyUnit": "kcal/mol",
        "cnnRange": 0.000005125900000013672,
        "officialAffinityPass": null,
        "jobs": [
          {
            "seed": 17,
            "status": "completed",
            "cnnScore": 0.9966449142,
            "energy": -9.97848,
            "cnnAffinity": 6.4814443588,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 17.026219,
            "wallSeconds": 11.778339497000161,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H0/state.json"
          },
          {
            "seed": 43,
            "status": "completed",
            "cnnScore": 0.9966500401,
            "energy": -9.98546,
            "cnnAffinity": 6.4787092209,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 17.821216,
            "wallSeconds": 12.053696676000072,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H0/state.json"
          }
        ]
      }
    },
    {
      "id": "H0-ev_tool_s2_gnina_01_CHEMBL1207227",
      "arm": "H0",
      "candidateId": "CHEMBL1207227",
      "kind": "actual_computational_results",
      "title": "GNINA paired calculation · CHEMBL1207227",
      "source": "runs/lab_comparison_v2/H0/state.json",
      "detail": "Recorded GNINA calculations with pose-validity and identity checks. CNNscore is a computational ranking signal, not biological activity. Qualified medians are absent when the paired-valid requirement is not met.",
      "record": {
        "protocolId": "lab_cdk2_gnina_p0profile_comparison_v2",
        "protocolHash": "69f1ee145a3bbced30d0e717d35dee296c4d2b0c823ce04f3bb848e9ae8637e0",
        "completedRepeats": 2,
        "validRepeats": 2,
        "pairedValid": true,
        "rawCnnMedian": 0.9604794681,
        "qualifiedCnnMedian": 0.9604794681,
        "rawEnergyMedian": -9.821745,
        "qualifiedEnergyMedian": -9.821745,
        "energyUnit": "kcal/mol",
        "cnnRange": 0.00021630520000004427,
        "officialAffinityPass": null,
        "jobs": [
          {
            "seed": 17,
            "status": "completed",
            "cnnScore": 0.9605876207,
            "energy": -9.97589,
            "cnnAffinity": 5.8956823349,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 26.615312,
            "wallSeconds": 22.727161049000188,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H0/state.json"
          },
          {
            "seed": 43,
            "status": "completed",
            "cnnScore": 0.9603713155,
            "energy": -9.6676,
            "cnnAffinity": 5.8206458092,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 26.131853000000003,
            "wallSeconds": 16.445628138999837,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H0/state.json"
          }
        ]
      }
    },
    {
      "id": "H0-ev_tool_s4_gnina_01_CHEMBL1241566",
      "arm": "H0",
      "candidateId": "CHEMBL1241566",
      "kind": "actual_computational_results",
      "title": "GNINA paired calculation · CHEMBL1241566",
      "source": "runs/lab_comparison_v2/H0/state.json",
      "detail": "Recorded GNINA calculations with pose-validity and identity checks. CNNscore is a computational ranking signal, not biological activity. Qualified medians are absent when the paired-valid requirement is not met.",
      "record": {
        "protocolId": "lab_cdk2_gnina_p0profile_comparison_v2",
        "protocolHash": "69f1ee145a3bbced30d0e717d35dee296c4d2b0c823ce04f3bb848e9ae8637e0",
        "completedRepeats": 2,
        "validRepeats": 2,
        "pairedValid": true,
        "rawCnnMedian": 0.9832958281,
        "qualifiedCnnMedian": 0.9832958281,
        "rawEnergyMedian": -9.681905,
        "qualifiedEnergyMedian": -9.681905,
        "energyUnit": "kcal/mol",
        "cnnRange": 0.003421366199999998,
        "officialAffinityPass": null,
        "jobs": [
          {
            "seed": 17,
            "status": "completed",
            "cnnScore": 0.981585145,
            "energy": -9.66394,
            "cnnAffinity": 7.2644944191,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 34.72514,
            "wallSeconds": 27.641064534999714,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H0/state.json"
          },
          {
            "seed": 43,
            "status": "completed",
            "cnnScore": 0.9850065112,
            "energy": -9.69987,
            "cnnAffinity": 7.3128728867,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 34.711796,
            "wallSeconds": 21.52959762799992,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H0/state.json"
          }
        ]
      }
    },
    {
      "id": "H1-ev_tool_s0_gnina_batch_01_CHEMBL102926",
      "arm": "H1",
      "candidateId": "CHEMBL102926",
      "kind": "actual_computational_results",
      "title": "GNINA paired calculation · CHEMBL102926",
      "source": "runs/lab_comparison_v2/H1/state.json",
      "detail": "Recorded GNINA calculations with pose-validity and identity checks. CNNscore is a computational ranking signal, not biological activity. Qualified medians are absent when the paired-valid requirement is not met.",
      "record": {
        "protocolId": "lab_cdk2_gnina_p0profile_comparison_v2",
        "protocolHash": "69f1ee145a3bbced30d0e717d35dee296c4d2b0c823ce04f3bb848e9ae8637e0",
        "completedRepeats": 2,
        "validRepeats": 0,
        "pairedValid": false,
        "rawCnnMedian": 0.9511046409499999,
        "qualifiedCnnMedian": null,
        "rawEnergyMedian": -8.416184999999999,
        "qualifiedEnergyMedian": null,
        "energyUnit": "kcal/mol",
        "cnnRange": 0.04895198349999996,
        "officialAffinityPass": null,
        "jobs": [
          {
            "seed": 17,
            "status": "completed",
            "cnnScore": 0.9266286492,
            "energy": -8.67488,
            "cnnAffinity": 7.9868850708,
            "poseValid": false,
            "identityPreserved": false,
            "failedChecks": [],
            "cpuSeconds": 68.031808,
            "wallSeconds": 47.06095069599996,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H1/state.json"
          },
          {
            "seed": 43,
            "status": "completed",
            "cnnScore": 0.9755806327,
            "energy": -8.15749,
            "cnnAffinity": 7.8927283287,
            "poseValid": false,
            "identityPreserved": false,
            "failedChecks": [],
            "cpuSeconds": 68.165551,
            "wallSeconds": 47.095059841999955,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H1/state.json"
          }
        ]
      }
    },
    {
      "id": "H1-ev_tool_s0_gnina_batch_01_CHEMBL194721",
      "arm": "H1",
      "candidateId": "CHEMBL194721",
      "kind": "actual_computational_results",
      "title": "GNINA paired calculation · CHEMBL194721",
      "source": "runs/lab_comparison_v2/H1/state.json",
      "detail": "Recorded GNINA calculations with pose-validity and identity checks. CNNscore is a computational ranking signal, not biological activity. Qualified medians are absent when the paired-valid requirement is not met.",
      "record": {
        "protocolId": "lab_cdk2_gnina_p0profile_comparison_v2",
        "protocolHash": "69f1ee145a3bbced30d0e717d35dee296c4d2b0c823ce04f3bb848e9ae8637e0",
        "completedRepeats": 2,
        "validRepeats": 2,
        "pairedValid": true,
        "rawCnnMedian": 0.99269136785,
        "qualifiedCnnMedian": 0.99269136785,
        "rawEnergyMedian": -9.114985,
        "qualifiedEnergyMedian": -9.114985,
        "energyUnit": "kcal/mol",
        "cnnRange": 0.004059851100000067,
        "officialAffinityPass": null,
        "jobs": [
          {
            "seed": 17,
            "status": "completed",
            "cnnScore": 0.9947212934,
            "energy": -9.30373,
            "cnnAffinity": 7.18116045,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 46.490281,
            "wallSeconds": 33.61312070300028,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H1/state.json"
          },
          {
            "seed": 43,
            "status": "completed",
            "cnnScore": 0.9906614423,
            "energy": -8.92624,
            "cnnAffinity": 7.2469830513,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 45.781691,
            "wallSeconds": 27.148944006999955,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H1/state.json"
          }
        ]
      }
    },
    {
      "id": "H1-ev_tool_s0_gnina_batch_01_CHEMBL1241567",
      "arm": "H1",
      "candidateId": "CHEMBL1241567",
      "kind": "actual_computational_results",
      "title": "GNINA paired calculation · CHEMBL1241567",
      "source": "runs/lab_comparison_v2/H1/state.json",
      "detail": "Recorded GNINA calculations with pose-validity and identity checks. CNNscore is a computational ranking signal, not biological activity. Qualified medians are absent when the paired-valid requirement is not met.",
      "record": {
        "protocolId": "lab_cdk2_gnina_p0profile_comparison_v2",
        "protocolHash": "69f1ee145a3bbced30d0e717d35dee296c4d2b0c823ce04f3bb848e9ae8637e0",
        "completedRepeats": 2,
        "validRepeats": 2,
        "pairedValid": true,
        "rawCnnMedian": 0.91990068555,
        "qualifiedCnnMedian": 0.91990068555,
        "rawEnergyMedian": -7.829445,
        "qualifiedEnergyMedian": -7.829445,
        "energyUnit": "kcal/mol",
        "cnnRange": 0.027980029500000003,
        "officialAffinityPass": null,
        "jobs": [
          {
            "seed": 17,
            "status": "completed",
            "cnnScore": 0.9338907003,
            "energy": -7.31572,
            "cnnAffinity": 7.0732722282,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 42.73235,
            "wallSeconds": 26.06064790499977,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H1/state.json"
          },
          {
            "seed": 43,
            "status": "completed",
            "cnnScore": 0.9059106708,
            "energy": -8.34317,
            "cnnAffinity": 7.0896420479,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 43.28846,
            "wallSeconds": 31.849351643999853,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H1/state.json"
          }
        ]
      }
    },
    {
      "id": "H1-ev_tool_s0_gnina_batch_01_CHEMBL50894",
      "arm": "H1",
      "candidateId": "CHEMBL50894",
      "kind": "actual_computational_results",
      "title": "GNINA paired calculation · CHEMBL50894",
      "source": "runs/lab_comparison_v2/H1/state.json",
      "detail": "Recorded GNINA calculations with pose-validity and identity checks. CNNscore is a computational ranking signal, not biological activity. Qualified medians are absent when the paired-valid requirement is not met.",
      "record": {
        "protocolId": "lab_cdk2_gnina_p0profile_comparison_v2",
        "protocolHash": "69f1ee145a3bbced30d0e717d35dee296c4d2b0c823ce04f3bb848e9ae8637e0",
        "completedRepeats": 2,
        "validRepeats": 2,
        "pairedValid": true,
        "rawCnnMedian": 0.99664747715,
        "qualifiedCnnMedian": 0.99664747715,
        "rawEnergyMedian": -9.98197,
        "qualifiedEnergyMedian": -9.98197,
        "energyUnit": "kcal/mol",
        "cnnRange": 0.000005125900000013672,
        "officialAffinityPass": null,
        "jobs": [
          {
            "seed": 17,
            "status": "completed",
            "cnnScore": 0.9966449142,
            "energy": -9.97848,
            "cnnAffinity": 6.4814443588,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 16.975073000000002,
            "wallSeconds": 11.6042181480002,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H1/state.json"
          },
          {
            "seed": 43,
            "status": "completed",
            "cnnScore": 0.9966500401,
            "energy": -9.98546,
            "cnnAffinity": 6.4787092209,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 18.857387,
            "wallSeconds": 18.3444059389999,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H1/state.json"
          }
        ]
      }
    },
    {
      "id": "H1-ev_tool_s2_gnina_batch_01_CHEMBL1241849",
      "arm": "H1",
      "candidateId": "CHEMBL1241849",
      "kind": "actual_computational_results",
      "title": "GNINA paired calculation · CHEMBL1241849",
      "source": "runs/lab_comparison_v2/H1/state.json",
      "detail": "Recorded GNINA calculations with pose-validity and identity checks. CNNscore is a computational ranking signal, not biological activity. Qualified medians are absent when the paired-valid requirement is not met.",
      "record": {
        "protocolId": "lab_cdk2_gnina_p0profile_comparison_v2",
        "protocolHash": "69f1ee145a3bbced30d0e717d35dee296c4d2b0c823ce04f3bb848e9ae8637e0",
        "completedRepeats": 2,
        "validRepeats": 2,
        "pairedValid": true,
        "rawCnnMedian": 0.64205130935,
        "qualifiedCnnMedian": 0.64205130935,
        "rawEnergyMedian": -9.301580000000001,
        "qualifiedEnergyMedian": -9.301580000000001,
        "energyUnit": "kcal/mol",
        "cnnRange": 0.10435503729999995,
        "officialAffinityPass": null,
        "jobs": [
          {
            "seed": 17,
            "status": "completed",
            "cnnScore": 0.694228828,
            "energy": -9.08103,
            "cnnAffinity": 6.8536119461,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 68.97403899999999,
            "wallSeconds": 47.678281522999896,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H1/state.json"
          },
          {
            "seed": 43,
            "status": "completed",
            "cnnScore": 0.5898737907,
            "energy": -9.52213,
            "cnnAffinity": 6.9678077698,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 67.56668499999999,
            "wallSeconds": 47.26617645399983,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H1/state.json"
          }
        ]
      }
    },
    {
      "id": "H1-ev_tool_s2_gnina_batch_01_CHEMBL362560",
      "arm": "H1",
      "candidateId": "CHEMBL362560",
      "kind": "actual_computational_results",
      "title": "GNINA paired calculation · CHEMBL362560",
      "source": "runs/lab_comparison_v2/H1/state.json",
      "detail": "Recorded GNINA calculations with pose-validity and identity checks. CNNscore is a computational ranking signal, not biological activity. Qualified medians are absent when the paired-valid requirement is not met.",
      "record": {
        "protocolId": "lab_cdk2_gnina_p0profile_comparison_v2",
        "protocolHash": "69f1ee145a3bbced30d0e717d35dee296c4d2b0c823ce04f3bb848e9ae8637e0",
        "completedRepeats": 2,
        "validRepeats": 1,
        "pairedValid": false,
        "rawCnnMedian": 0.47194273769999995,
        "qualifiedCnnMedian": null,
        "rawEnergyMedian": -11.558254999999999,
        "qualifiedEnergyMedian": null,
        "energyUnit": "kcal/mol",
        "cnnRange": 0.3979472816,
        "officialAffinityPass": null,
        "jobs": [
          {
            "seed": 17,
            "status": "completed",
            "cnnScore": 0.2729690969,
            "energy": -10.93711,
            "cnnAffinity": 6.9387550354,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 67.52559600000001,
            "wallSeconds": 46.27897854999992,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H1/state.json"
          },
          {
            "seed": 43,
            "status": "completed",
            "cnnScore": 0.6709163785,
            "energy": -12.1794,
            "cnnAffinity": 7.205634594,
            "poseValid": false,
            "identityPreserved": true,
            "failedChecks": [
              "internal_steric_clash",
              "internal_energy"
            ],
            "cpuSeconds": 69.283989,
            "wallSeconds": 47.406531408999854,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H1/state.json"
          }
        ]
      }
    },
    {
      "id": "H1-ev_tool_s2_gnina_batch_01_CHEMBL1207227",
      "arm": "H1",
      "candidateId": "CHEMBL1207227",
      "kind": "actual_computational_results",
      "title": "GNINA paired calculation · CHEMBL1207227",
      "source": "runs/lab_comparison_v2/H1/state.json",
      "detail": "Recorded GNINA calculations with pose-validity and identity checks. CNNscore is a computational ranking signal, not biological activity. Qualified medians are absent when the paired-valid requirement is not met.",
      "record": {
        "protocolId": "lab_cdk2_gnina_p0profile_comparison_v2",
        "protocolHash": "69f1ee145a3bbced30d0e717d35dee296c4d2b0c823ce04f3bb848e9ae8637e0",
        "completedRepeats": 2,
        "validRepeats": 2,
        "pairedValid": true,
        "rawCnnMedian": 0.9604794681,
        "qualifiedCnnMedian": 0.9604794681,
        "rawEnergyMedian": -9.821745,
        "qualifiedEnergyMedian": -9.821745,
        "energyUnit": "kcal/mol",
        "cnnRange": 0.00021630520000004427,
        "officialAffinityPass": null,
        "jobs": [
          {
            "seed": 17,
            "status": "completed",
            "cnnScore": 0.9605876207,
            "energy": -9.97589,
            "cnnAffinity": 5.8956823349,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 26.631366999999997,
            "wallSeconds": 22.77492809799969,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H1/state.json"
          },
          {
            "seed": 43,
            "status": "completed",
            "cnnScore": 0.9603713155,
            "energy": -9.6676,
            "cnnAffinity": 5.8206458092,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 25.805984000000002,
            "wallSeconds": 16.197822584000278,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H1/state.json"
          }
        ]
      }
    },
    {
      "id": "H1-ev_tool_s4_gnina_batch_01_CHEMBL258805",
      "arm": "H1",
      "candidateId": "CHEMBL258805",
      "kind": "actual_computational_results",
      "title": "GNINA paired calculation · CHEMBL258805",
      "source": "runs/lab_comparison_v2/H1/state.json",
      "detail": "Recorded GNINA calculations with pose-validity and identity checks. CNNscore is a computational ranking signal, not biological activity. Qualified medians are absent when the paired-valid requirement is not met.",
      "record": {
        "protocolId": "lab_cdk2_gnina_p0profile_comparison_v2",
        "protocolHash": "69f1ee145a3bbced30d0e717d35dee296c4d2b0c823ce04f3bb848e9ae8637e0",
        "completedRepeats": 2,
        "validRepeats": 2,
        "pairedValid": true,
        "rawCnnMedian": 0.98735496405,
        "qualifiedCnnMedian": 0.98735496405,
        "rawEnergyMedian": -7.967040000000001,
        "qualifiedEnergyMedian": -7.967040000000001,
        "energyUnit": "kcal/mol",
        "cnnRange": 0.0036378503000000784,
        "officialAffinityPass": null,
        "jobs": [
          {
            "seed": 17,
            "status": "completed",
            "cnnScore": 0.9891738892,
            "energy": -7.9884,
            "cnnAffinity": 5.9353756905,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 18.230591,
            "wallSeconds": 17.522970229999828,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H1/state.json"
          },
          {
            "seed": 43,
            "status": "completed",
            "cnnScore": 0.9855360389,
            "energy": -7.94568,
            "cnnAffinity": 5.8525061607,
            "poseValid": true,
            "identityPreserved": true,
            "failedChecks": [],
            "cpuSeconds": 17.193533,
            "wallSeconds": 11.376708262000193,
            "gpuObserved": true,
            "source": "runs/lab_comparison_v2/H1/state.json"
          }
        ]
      }
    }
  ],
  "meta": {
    "status": "candidate_not_released",
    "releaseAuthorized": false,
    "inputTokens": 99669,
    "outputTokens": 5362,
    "totalTokens": 105031,
    "wallSeconds": 137.1844825000735,
    "presentationMode": "compact_json_v1",
    "summary": "Meta proposed a single change to task-input presentation: remove JSON whitespace outside strings while preserving the complete logical payload. H1 was evaluated as a candidate; release is not authorized.",
    "changes": [
      "Remove formatting whitespace outside JSON strings only.",
      "Preserve values, types, member order, candidate order, arrays, duplicate records, missing values and source references.",
      "Keep the model, tools, protocol, ranking objective, budgets and non-JSON prompt text unchanged.",
      "Retain original input artifacts and record the actual rendered prompt separately."
    ],
    "source": "runs/meta_h0_to_h1_v1/proposal_binding.json"
  },
  "audit": {
    "passed": true,
    "status": "passed",
    "source": "runs/lab_comparison_v2_audit/audit.json",
    "auditedAt": "2026-10-20T07:16:38.305997+00:00",
    "crossArmReuseDetected": false,
    "executionOrder": [
      "baseline",
      "H0",
      "H1"
    ],
    "firstInputEqualityPassed": true,
    "memoryReviewStateModified": false,
    "scope": "Artifact bindings, input replay and execution records passed recorded checks. This is not blinded replication or proof of biological efficacy; scientific memory remains pending review.",
    "sourceHashScope": "recorded_binding means this import checked a hash recorded in another source artifact; import_hash_only identifies a local import root without an upstream pinned hash. These checks do not establish independent provenance of the root artifacts. Meta usage also matches the preregistration-bound state call."
  },
  "sources": [
    {
      "path": "data/lab_cdk2_v2/candidates.json",
      "sha256": "295fbe2fdd3237406c7f4c2b43d50f8880a7364a420368c03023fab4e769079d",
      "verification": "recorded_binding"
    },
    {
      "path": "data/lab_cdk2_v2/manifest.json",
      "sha256": "501d82871ff14889b64867888bcb39ee722258785f945226b181ca2dd3ccca3b",
      "verification": "recorded_binding"
    },
    {
      "path": "protocols/lab_comparison_v2.json",
      "sha256": "69f1ee145a3bbced30d0e717d35dee296c4d2b0c823ce04f3bb848e9ae8637e0",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/lab_comparison_v2/H0/evaluation.json",
      "sha256": "1737fcc1f6051020da8b4a00a85b02ff1485fa4ee0a13321b15509119c73467b",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/lab_comparison_v2/H0/state.json",
      "sha256": "5f82b525983c1e2329b587c19f5e7168d54812dd53262d21db971f5fd0b71c11",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/lab_comparison_v2/H1/evaluation.json",
      "sha256": "874ac5a1cc7da59c2705881d0ae63ed63727a3924e5083390193ec1300bb0d60",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/lab_comparison_v2/H1/state.json",
      "sha256": "340aa84953f5f1a3a8ccf745bf4f8aff0c98e878e841bd6815382057c9967247",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/lab_comparison_v2/fixed_baseline/audit.json",
      "sha256": "ab71d30d8c1809fcd9f10fff531c0d809631d080633a9927c57a7b3c130e531a",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_001/artifact_hashes.json",
      "sha256": "8b88d03563f3fd34eb0170725b60c18e6210f4393bd288b4c49b40f0e9cb1ede",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_001/jobs/CHEMBL102926_s17/result.json",
      "sha256": "f335f27b69fe0cb31ba7a4e6c3d023248dc645e74cd22b865d938ba51759be35",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_001/jobs/CHEMBL102926_s43/result.json",
      "sha256": "d0eb94127da8ac467aea8c1733f228afd9d7089b78869ed77c54b1d70e76a3d6",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_001/jobs/CHEMBL1241567_s17/result.json",
      "sha256": "7cfaea6d5ba44d85acef8e014421b16a225a1fc112613e06d688bf3b781a2930",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_001/jobs/CHEMBL1241567_s43/result.json",
      "sha256": "f8ee5d3dee188d5552e923cb8d41cb370c1475dad7e24cf98b6b7f71502be244",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_001/jobs/CHEMBL1241849_s17/result.json",
      "sha256": "18a4338d6ecb77bdf0d917b794d04cd5222c531ecee1f1d0703ade6707908c09",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_001/jobs/CHEMBL1241849_s43/result.json",
      "sha256": "da71a9a67461e805a0c7f363ed5ca37926ede40539edfc3a1d8be56cfa9b061a",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_001/jobs/CHEMBL194721_s17/result.json",
      "sha256": "44544eb7e9145b6259befef56ed0a11f2640879dee2ef4871ccac3185d1adae0",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_001/jobs/CHEMBL194721_s43/result.json",
      "sha256": "0c3d7f4b1f7db6f0d48a21c861a3825b7013d31f68547b8e9288ba23b3b42ff5",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_002/artifact_hashes.json",
      "sha256": "d1b3b5c5f0d87b6127dfd873ceafbd1db9ef33d431f5297af5af36121a65c8dd",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_002/jobs/CHEMBL1207227_s17/result.json",
      "sha256": "654ea8aa857ee1160b0126e8ac5f918b4d1b4cffe29cf104846b57efbf46940d",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_002/jobs/CHEMBL1207227_s43/result.json",
      "sha256": "3056b69f7e3b0128a987f2c0e4709e10bb6a7752425d8af3185dc19eb6633dd9",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_002/jobs/CHEMBL1241566_s17/result.json",
      "sha256": "93f902db7d83b44d87961e55dbde386c3da75957ea50ee279f5f086258e0a53c",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_002/jobs/CHEMBL1241566_s43/result.json",
      "sha256": "ea373f71b58a5bb86321af1c6acd203e3c26939638649604713e6125f5831506",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_002/jobs/CHEMBL362560_s17/result.json",
      "sha256": "7be00f3436fddc79b7a470b07015d30add610980208ccad605c128fae182c226",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_002/jobs/CHEMBL362560_s43/result.json",
      "sha256": "1d398762eff98f1eb7afa55d610be14dff9e5f436820a1bdcde5c004b5d755f1",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_002/jobs/CHEMBL50894_s17/result.json",
      "sha256": "a71d5cc75eb0ae9b6c945122cf1d494634ef1185c92384362395620e06ee44ef",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/lab_comparison_v2/fixed_baseline/batches/baseline_002/jobs/CHEMBL50894_s43/result.json",
      "sha256": "3592726409c352838f3a2485a1602f1ab5075e8fdcbfc09be1f0148fed85fb12",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/lab_comparison_v2/fixed_baseline/evaluation.json",
      "sha256": "b251c7017cfbc530af448396f069f2af447fc05d9efb2a99523ea485fb2cfc38",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/lab_comparison_v2/fixed_baseline/state.json",
      "sha256": "79a942b9de2d0fb77a2397ff58048cc6beb11f851a7a90c8680ac0a83c636419",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/lab_comparison_v2/preregistration.json",
      "sha256": "1326d2129a0433307f1a0a245076f1ca708c5ee782b7e21a285587817cb40101",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/lab_comparison_v2_audit/H0_audit.json",
      "sha256": "84bd6d0429e6ce0ca83d85df1028134cfd488738d9934b3c324afa15026a2566",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/lab_comparison_v2_audit/H1_audit.json",
      "sha256": "0838d7931477686e5ec5627e656febe9acddab460031fe21ff17a4f12b5be071",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/lab_comparison_v2_audit/audit.json",
      "sha256": "9bf43dc2c8f401f86d884d0b33f758c6c2dc947154f05da5226b083393419940",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/lab_comparison_v2_readout/summary.json",
      "sha256": "0591cf485063f7319a8497e985a074238645eb00ee3e427033fba9022bd204ec",
      "verification": "import_hash_only"
    },
    {
      "path": "runs/meta_h0_to_h1_v1/artifact_hashes.json",
      "sha256": "98713851bf6777abcb28138a8217aecd81bc251ae6470dfbe0b7bdb21d64814a",
      "verification": "import_hash_only"
    },
    {
      "path": "runs/meta_h0_to_h1_v1/h1_config.json",
      "sha256": "37ae2af93a664d7990a2863fe736363936fce30b50216027632e8830d00b616b",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/meta_h0_to_h1_v1/proposal_binding.json",
      "sha256": "43057d21b9982424235a0ffc6d39ba56413ecb929b92424ae04e4b6b690aba9f",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/meta_h0_to_h1_v1/response.json",
      "sha256": "c76aceb25aa46300e7b1f7cb6ae6c4cd26773726fef718b12ede7e1fbd387d6d",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/meta_h0_to_h1_v1/state.json",
      "sha256": "4f336424b568dde94a0762330bb152775254f84eace64d5b5c918df9d4db38a3",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/meta_h0_to_h1_v1/usage.json",
      "sha256": "274c46281c6620e29d05c581382ae65eea37fe790d9a1cddd20f57727f25863c",
      "verification": "recorded_binding"
    },
    {
      "path": "runs/meta_h0_to_h1_v1_review/controller_review.json",
      "sha256": "899adbd133f309c8ab5f07bfabff894024b1e6324fcebe9f327f7819dcbd2aee",
      "verification": "recorded_binding"
    }
  ],
  "limitations": [
    "One pool and one stochastic trajectory per agent arm do not establish significance, generalization or causal improvement.",
    "CNNscore is a GNINA computational ranking signal, not a probability of biological activity. No calibrated affinity pass threshold was established.",
    "The 5/5 paired-valid result describes the final five candidates only; it is distinct from validity across all executed poses and from biological validation.",
    "Execution order was fixed: baseline, H0, then H1. Time drift remains a limitation.",
    "H1 token usage excludes the separate Meta invocation. H1 plus Meta has higher observed token usage than H0 in this experiment; no cost payback is established.",
    "Token counts include observed CLI input and output only. Cached and reasoning subtotals are not added twice; outer development and audit conversations are excluded. USD cost is unknown.",
    "Batch wall time and observed GPU process presence do not measure GPU active time or GPU-hours.",
    "Public historical IC50 values were visible to all arms. Calculations do not constitute new wet-lab measurements.",
    "No result is shown for an uncomputed candidate-arm pair. A missing result is not a zero score or a failed molecule.",
    "H1 remains an unreleased candidate. The recorded audit does not authorize deployment or erase pending scientific-memory review states.",
    "The assay has a homologous single-protein target assignment; receptor and assay context limit biological interpretation.",
    "This portable snapshot is a readout of completed artifacts, not a live connection to agents or GPU workers."
  ]
};

---
license: other
pretty_name: PubTables-1M-OTSL
size_categories:
- 100K<n<1M
tags:
- table-structure-recognition
- table-understanding
- PDF
task_categories:
- object-detection
- table-to-text
---
# Dataset Card for PubTables-1M_OTSL

## Dataset Description

- **Homepage:** https://ds4sd.github.io
- **Paper:** https://arxiv.org/pdf/2305.03393

### Dataset Summary
This dataset enables the evaluation of both object detection models and image-to-text methods.
[PubTables-1M](https://github.com/microsoft/table-transformer) is introduced in the publication *"PubTables-1M: Towards Comprehensive Table Extraction From Unstructured Documents"* by Smock et al. The conversion into HF (Hugging Face) and the addition of the OTSL (Optimized Table Structure Language) format is presented in our paper "Optimized Table Tokenization for Table Structure Recognition" by Lysak et al. The dataset includes the original annotations amongst new additions. 

### Dataset Structure

* cells: origunal dataset cell groundtruth (content).
* table_bbox: origunal dataset table detection groundtruth.
* otsl: new reduced table structure token format
* html: Generated HTML for PubTables-1M to match PubTabNet, FinTabNet, and SynthTabNet format.
* html_restored: generated HTML from OTSL.
* cols: grid column length.
* rows: grid row length.
* image: PIL image

### OTSL Vocabulary:

**OTSL**: new reduced table structure token format
More information on the OTSL table structure format and its concepts can be read from our paper.
Format of this dataset extends work presented in a paper, and introduces slight modifications:

* "fcel" - cell that has content in it
* "ecel" - cell that is empty
* "lcel" - left-looking cell (to handle horizontally merged cells)
* "ucel" - up-looking cell (to handle vertically merged cells)
* "xcel" - 2d span cells, in this dataset - covers entire area of a merged cell
* "nl" - new line token

### Data Splits

The dataset provides three splits
- `train`
- `val`
- `test`

## Additional Information

### Dataset Curators

The dataset is converted by the [Deep Search team](https://ds4sd.github.io/) at IBM Research.
You can contact us at [deepsearch-core@zurich.ibm.com](mailto:deepsearch-core@zurich.ibm.com).

Curators:
- Maksym Lysak, [@maxmnemonic](https://github.com/maxmnemonic)
- Ahmed Nassar, [@nassarofficial](https://github.com/nassarofficial)
- Christoph Auer, [@cau-git](https://github.com/cau-git)
- Nikos Livathinos, [@nikos-livathinos](https://github.com/nikos-livathinos)
- Peter Staar, [@PeterStaar-IBM](https://github.com/PeterStaar-IBM)


### Citation Information

**Citation to OTSL Paper:**

	@article{lysak2023optimized,
	      title={Optimized Table Tokenization for Table Structure Recognition}, 
	      author={Maksym Lysak and Ahmed Nassar and Nikolaos Livathinos and Christoph Auer and Peter Staar},
	      year={2023},
	      eprint={2305.03393},
	      archivePrefix={arXiv},
	      primaryClass={cs.CV}
	}

**Citation to PubTables-1M creators:**

	@inproceedings{smock2022pubtables,
	  title={Pub{T}ables-1{M}: Towards comprehensive table extraction from unstructured documents},
	  author={Smock, Brandon and Pesala, Rohith and Abraham, Robin},
	  booktitle={Proceedings of the IEEE/CVF Conference on Computer Vision and Pattern Recognition (CVPR)},
	  pages={4634-4642},
	  year={2022},
	  month={June}
	}
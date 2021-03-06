""" Genes """
import threading
import sys
import numpy as np

from AnyQt.QtWidgets import (
    QSplitter, QTableView,  QHeaderView, QAbstractItemView, QStyle, QApplication
)
from AnyQt.QtCore import (
    Qt, QSize, QThreadPool, QAbstractTableModel, QVariant, QModelIndex
)
from AnyQt.QtGui import (
    QFont, QColor
)

from Orange.widgets.gui import (
    vBox, comboBox, ProgressBar, widgetBox, auto_commit, widgetLabel, checkBox,
    rubber, lineEdit, LinkRole, LinkStyledItemDelegate
)
from Orange.widgets.widget import OWWidget
from Orange.widgets.utils import itemmodels
from Orange.widgets.settings import Setting, DomainContextHandler, ContextSetting
from Orange.widgets.utils.signals import Output, Input
from Orange.data import StringVariable, DiscreteVariable, Domain, Table, filter as table_filter


from orangecontrib.bioinformatics.widgets.utils.data import (
    TAX_ID, GENE_AS_ATTRIBUTE_NAME, GENE_ID_COLUMN, GENE_ID_ATTRIBUTE
)
from orangecontrib.bioinformatics.widgets.utils.concurrent import Worker
from orangecontrib.bioinformatics.ncbi import taxonomy
from orangecontrib.bioinformatics.ncbi.gene import GeneMatcher, NCBI_ID, GENE_MATCHER_HEADER, NCBI_DETAIL_LINK

from functools import lru_cache


class GeneInfoModel(itemmodels.PyTableModel):
    def __init__(self,  *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.header_labels, self.gene_attributes = GENE_MATCHER_HEADER
        self.setHorizontalHeaderLabels(self.header_labels)

        try:
            # note: make sure ncbi_id is set in GENE_MATCHER_HEADER
            self.entrez_column_index = self.gene_attributes.index('ncbi_id')
        except ValueError as e:
            raise ValueError("Make sure 'ncbi_id' is set in gene.GENE_MATCHER_HEADER")

        self.genes = None
        self.data_table = None

        self.font = QFont()
        self.font.setUnderline(True)
        self.color = QColor(Qt.blue)

        @lru_cache(maxsize=10000)
        def _row_instance(row, column):
            return self[int(row)][int(column)]
        self._row_instance = _row_instance

    def initialize(self, list_of_genes):
        self.genes = list_of_genes
        self.__table_from_genes([gene for gene in list_of_genes if gene.ncbi_id])
        self.show_table()

    def columnCount(self, parent=QModelIndex()):
        return 0 if (parent.isValid() or self._table.size == 0) else self._table.shape[1]

    def clear(self):
        self.beginResetModel()
        self._table = np.array([[]])
        self.resetSorting()
        self._roleData.clear()
        self.endResetModel()

    def data(self, index, role,
             _str=str,
             _Qt_DisplayRole=Qt.DisplayRole,
             _Qt_EditRole=Qt.EditRole,
             _Qt_FontRole=Qt.FontRole,
             _Qt_ForegroundRole=Qt.ForegroundRole,
             _LinkRolee=LinkRole,
             _recognizedRoles=frozenset([Qt.DisplayRole, Qt.EditRole, Qt.FontRole, Qt.ForegroundRole, LinkRole])):

        if role not in _recognizedRoles:
            return None

        row, col = index.row(), index.column()
        if not 0 <= row <= self.rowCount():
            return None
        row = self.mapToSourceRows(row)

        try:
            # value = self[row][col]
            value = self._row_instance(row, col)
        except IndexError:
            return

        if role == Qt.DisplayRole:
            return QVariant(str(value))
        elif role == Qt.ToolTipRole:
            return QVariant(str(value))

        if col == self.entrez_column_index:
            if role == _Qt_ForegroundRole:
                return self.color
            elif role == _Qt_FontRole:
                return self.font
            elif role == _LinkRolee:
                return NCBI_DETAIL_LINK.format(value)

    def __table_from_genes(self, list_of_genes):
        # type: (list) -> None
        self.data_table = np.asarray([gene.to_list() for gene in list_of_genes])

    def filter_table(self, filter_pattern):
        _, col_size = self.data_table.shape
        invalid_result = np.array([-1 for _ in range(col_size)])

        filtered_rows = []
        for row in self.data_table:
            match_result = np.core.defchararray.rfind(np.char.lower(row), filter_pattern.lower())
            filtered_rows.append(not np.array_equal(match_result, invalid_result))
        return filtered_rows

    def show_table(self, filter_pattern=None):
        # Don't call filter if filter_pattern is empty
        if filter_pattern:
            # clear cache if model changes
            self._row_instance.cache_clear()
            self.wrap(self.data_table[self.filter_table(filter_pattern)])
        else:
            self.wrap(self.data_table)


class UnknownGeneInfoModel(itemmodels.PyListModel):
    def __init__(self,  *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.header_labels = ['IDs from the input data without corresponding Entrez ID']
        self.genes = []

    def initialize(self, list_of_genes):
        self.genes = list_of_genes
        self.wrap([', '.join([gene.input_name for gene in list_of_genes if not gene.ncbi_id])])

    def data(self, index, role=Qt.DisplayRole):
        row = index.row()
        if role in [self.list_item_role, Qt.EditRole] and self._is_index_valid(index):
            return self[row]
        elif role == Qt.TextAlignmentRole:
            return Qt.AlignLeft | Qt.AlignTop
        elif self._is_index_valid(row):
            return self._other_data[row].get(role, None)

    def headerData(self, section, orientation, role=Qt.DisplayRole):

        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.header_labels[section]
        return QAbstractTableModel.headerData(self, section, orientation, role)


class OWGenes(OWWidget):
    name = "Genes"
    description = "Tool for working with genes"
    icon = "../widgets/icons/OWGeneInfo.svg"
    priority = 5
    want_main_area = True

    selected_organism = Setting(11)

    search_pattern = Setting('')
    exclude_unmatched = Setting(True)
    replace_id_with_symbol = Setting(True)
    auto_commit = Setting(True)

    settingsHandler = DomainContextHandler()
    selected_gene_col = ContextSetting(None)
    use_attr_names = ContextSetting(True)

    replaces = [
        'orangecontrib.bioinformatics.widgets.OWGeneNameMatcher.OWGeneNameMatcher'
    ]

    class Inputs:
        data_table = Input("Data", Table)

    class Outputs:
        data_table = Output("Data", Table)
        gene_matcher_results = Output("Genes", Table)

    class Information(OWWidget.Information):
        pass

    def sizeHint(self):
        return QSize(1280, 960)

    def __init__(self):
        super().__init__()
        # ATTRIBUTES #
        self.target_database = NCBI_ID

        # input data
        self.input_data = None
        self.input_genes = None
        self.tax_id = None
        self.column_candidates = []

        # input options
        self.organisms = []

        # gene matcher
        self.gene_matcher = None

        # threads
        self.threadpool = QThreadPool(self)
        self.workers = None

        # progress bar
        self.progress_bar = None

        # GUI SECTION #

        # Control area
        self.info_box = widgetLabel(
            widgetBox(self.controlArea, "Info", addSpace=True), 'No data on input.\n'
        )

        organism_box = vBox(self.controlArea, 'Organism')
        self.organism_select_combobox = comboBox(organism_box, self,
                                                 'selected_organism',
                                                 callback=self.on_input_option_change)

        self.get_available_organisms()
        self.organism_select_combobox.setCurrentIndex(self.selected_organism)

        box = widgetBox(self.controlArea, 'Gene IDs in the input data')
        self.gene_columns_model = itemmodels.DomainModel(valid_types=(StringVariable, DiscreteVariable))
        self.gene_column_combobox = comboBox(box, self, 'selected_gene_col',
                                             label='Stored in data column',
                                             model=self.gene_columns_model,
                                             sendSelectedValue=True,
                                             callback=self.on_input_option_change)

        self.attr_names_checkbox = checkBox(box, self, 'use_attr_names', 'Stored as feature (column) names',
                                            disables=[(-1, self.gene_column_combobox)],
                                            callback=self.on_input_option_change)

        self.gene_column_combobox.setDisabled(bool(self.use_attr_names))

        output_box = vBox(self.controlArea, 'Output')

        # separator(output_box)
        # output_box.layout().addWidget(horizontal_line())
        # separator(output_box)
        self.exclude_radio = checkBox(output_box, self,
                                      'exclude_unmatched',
                                      'Exclude unmatched genes',
                                      callback=self.commit)

        self.replace_radio = checkBox(output_box, self,
                                      'replace_id_with_symbol',
                                      'Replace feature IDs with gene names',
                                      callback=self.commit)

        auto_commit(self.controlArea, self, "auto_commit", "&Commit", box=False)

        rubber(self.controlArea)

        # Main area
        self.filter = lineEdit(self.mainArea, self,
                               'search_pattern', 'Filter:',
                               callbackOnType=True, callback=self.apply_filter)
        # rubber(self.radio_group)
        self.mainArea.layout().addWidget(self.filter)

        # set splitter
        self.splitter = QSplitter()
        self.splitter.setOrientation(Qt.Vertical)

        self.table_model = GeneInfoModel()
        self.table_view = QTableView()
        self.table_view.setAlternatingRowColors(True)
        self.table_view.viewport().setMouseTracking(True)
        self.table_view.setSortingEnabled(True)
        self.table_view.setShowGrid(False)
        self.table_view.verticalHeader().hide()
        # self.table_view.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

        self.unknown_model = UnknownGeneInfoModel()

        self.unknown_view = QTableView()
        self.unknown_view.setModel(self.unknown_model)
        self.unknown_view.verticalHeader().hide()
        self.unknown_view.setShowGrid(False)
        self.unknown_view.setSelectionMode(QAbstractItemView.NoSelection)
        self.unknown_view.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

        self.splitter.addWidget(self.table_view)
        self.splitter.addWidget(self.unknown_view)

        self.splitter.setStretchFactor(0, 90)
        self.splitter.setStretchFactor(1, 10)

        self.mainArea.layout().addWidget(self.splitter)

    def apply_filter(self):
        # filter only if input data is present and model is populated
        if self.input_data is not None and self.table_model.data_table is not None:
            self.table_view.clearSpans()
            self.table_view.setModel(None)
            self.table_view.setSortingEnabled(False)
            self.table_model.show_table(str(self.search_pattern))
            self.table_view.setModel(self.table_model)
            self.table_view.selectionModel().selectionChanged.connect(self.commit)
            self.table_view.setSortingEnabled(True)

    def __reset_widget_state(self):
        self.table_view.clearSpans()
        self.table_view.setModel(None)
        self.table_model.clear()
        self.unknown_model.clear()
        self._update_info_box()

    def __selection_changed(self):
        genes = [model_index.data() for model_index in self.extended_view.get_selected_gens()]
        self.extended_view.set_info_model(genes)

    def _update_info_box(self):

        if self.input_genes and self.gene_matcher:
            num_genes = len(self.gene_matcher.genes)
            known_genes = len(self.gene_matcher.get_known_genes())

            info_text = '{} genes in input data\n' \
                        '{} genes match Entrez database\n' \
                        '{} genes with match conflicts\n'.format(num_genes, known_genes, num_genes - known_genes)

        else:
            info_text = 'No data on input.'

        self.info_box.setText(info_text)

    def _progress_advance(self):
        # GUI should be updated in main thread. That's why we are calling advance method here
        if self.progress_bar:
            self.progress_bar.advance()

    def _handle_matcher_results(self):
        assert threading.current_thread() == threading.main_thread()

        if self.progress_bar:
            self.progress_bar.finish()
            self.setStatusMessage('')

        # update info box
        self._update_info_box()

        # set output options
        self.toggle_radio_options()

        # set known genes
        self.table_model.initialize(self.gene_matcher.genes)
        self.table_view.setModel(self.table_model)
        self.table_view.selectionModel().selectionChanged.connect(self.commit)
        self.table_view.setSelectionBehavior(QAbstractItemView.SelectRows)

        self.table_view.setItemDelegateForColumn(
            self.table_model.entrez_column_index, LinkStyledItemDelegate(self.table_view)
        )
        v_header = self.table_view.verticalHeader()
        option = self.table_view.viewOptions()
        size = self.table_view.style().sizeFromContents(
            QStyle.CT_ItemViewItem, option,
            QSize(20, 20), self.table_view)

        v_header.setDefaultSectionSize(size.height() + 2)
        v_header.setMinimumSectionSize(5)
        self.table_view.horizontalHeader().setStretchLastSection(True)

        # set unknown genes
        self.unknown_model.initialize(self.gene_matcher.genes)
        self.unknown_view.verticalHeader().setStretchLastSection(True)

        self.commit()

    def get_available_organisms(self):
        available_organism = sorted([(tax_id, taxonomy.name(tax_id)) for tax_id in taxonomy.common_taxids()],
                                    key=lambda x: x[1])

        self.organisms = [tax_id[0] for tax_id in available_organism]
        self.organism_select_combobox.addItems([tax_id[1] for tax_id in available_organism])

    def gene_names_from_table(self):
        """ Extract and return gene names from `Orange.data.Table`.
        """
        self.input_genes = []
        if self.input_data:
            if self.use_attr_names:
                self.input_genes = [str(attr.name).strip() for attr in self.input_data.domain.attributes]
            else:
                if self.selected_gene_col is None:
                    self.selected_gene_col = self.gene_column_identifier()

                self.input_genes = [str(e[self.selected_gene_col]) for e in self.input_data
                                    if not np.isnan(e[self.selected_gene_col])]

    def _update_gene_matcher(self):
        self.gene_names_from_table()
        self.gene_matcher = GeneMatcher(self.get_selected_organism(), case_insensitive=True)
        self.gene_matcher.genes = self.input_genes
        self.gene_matcher.organism = self.get_selected_organism()

    def get_selected_organism(self):
        return self.organisms[self.selected_organism]

    def match_genes(self):
        if self.gene_matcher:
            # init progress bar
            self.progress_bar = ProgressBar(self, iterations=len(self.gene_matcher.genes))
            # status message
            self.setStatusMessage('Gene matcher running')

            worker = Worker(self.gene_matcher.run_matcher, progress_callback=True)
            worker.signals.progress.connect(self._progress_advance)
            worker.signals.finished.connect(self._handle_matcher_results)

            # move download process to worker thread
            self.threadpool.start(worker)

    def on_input_option_change(self):
        self.__reset_widget_state()
        self._update_gene_matcher()
        self.match_genes()

    def gene_column_identifier(self):
        """
        Get most suitable column that stores genes. If there are
        several suitable columns, select the one with most unique
        values. Take the best one.
        """

        # candidates -> (variable, num of unique values)
        candidates = ((col, np.unique(self.input_data.get_column_view(col)[0]).size)
                      for col in self.gene_columns_model
                      if isinstance(col, DiscreteVariable) or isinstance(col, StringVariable))

        best_candidate, _ = sorted(candidates, key=lambda x: x[1])[-1]
        return best_candidate

    def find_genes_location(self):
        """ Try locate the genes in the input data when we first load the data.

            Proposed rules:
                - when no suitable feature names are present, check the columns.
                - find the most suitable column, that is, the one with most unique values.

        """
        domain = self.input_data.domain
        if not domain.attributes:
            if self.selected_gene_col is None:
                self.selected_gene_col = self.gene_column_identifier()
                self.use_attr_names = False

    @Inputs.data_table
    def handle_input(self, data):
        self.closeContext()
        self.input_data = None
        self.input_genes = None
        self.__reset_widget_state()
        self.gene_columns_model.set_domain(None)
        self.selected_gene_col = None

        if data:
            self.input_data = data
            self.gene_columns_model.set_domain(self.input_data.domain)

            # check if input table has tax_id, human is used if tax_id is not found
            self.tax_id = str(self.input_data.attributes.get(TAX_ID, '9606'))
            # check for gene location. Default is that genes are attributes in the input table.
            self.use_attr_names = self.input_data.attributes.get(GENE_AS_ATTRIBUTE_NAME, self.use_attr_names)

            if self.tax_id in self.organisms:
                self.selected_organism = self.organisms.index(self.tax_id)

            self.openContext(self.input_data.domain)
            self.find_genes_location()
            self.on_input_option_change()

    def commit(self):
        selection = self.table_view.selectionModel().selectedRows(self.table_model.entrez_column_index)
        selected_genes = [row.data() for row in selection]
        gene_ids = self.get_target_ids()
        known_genes = [gid for gid in gene_ids if gid != '?']

        table = None
        gm_table = None
        if known_genes:
            # Genes are in rows (we have a column with genes).
            if not self.use_attr_names:

                if self.target_database in self.input_data.domain:
                    gene_var = self.input_data.domain[self.target_database]
                    metas = self.input_data.domain.metas
                else:
                    gene_var = StringVariable(self.target_database)
                    metas = self.input_data.domain.metas + (gene_var,)

                domain = Domain(self.input_data.domain.attributes,
                                self.input_data.domain.class_vars,
                                metas)

                table = self.input_data.transform(domain)
                col, _ = table.get_column_view(gene_var)
                col[:] = gene_ids

                # filter selected rows
                selected_rows = [row_index for row_index, row in enumerate(table)
                                 if str(row[gene_var]) in selected_genes]

                # handle table attributes
                table.attributes[TAX_ID] = self.get_selected_organism()
                table.attributes[GENE_AS_ATTRIBUTE_NAME] = False
                table.attributes[GENE_ID_COLUMN] = self.target_database
                table = table[selected_rows] if selected_rows else table

                if self.exclude_unmatched:
                    # create filter from selected column for genes
                    only_known = table_filter.FilterStringList(gene_var, known_genes)
                    # apply filter to the data
                    table = table_filter.Values([only_known])(table)

                self.Outputs.data_table.send(table)

            # genes are are in columns (genes are features).
            else:
                domain = self.input_data.domain.copy()
                table = self.input_data.transform(domain)

                for gene in self.gene_matcher.genes:
                    if gene.input_name in table.domain:

                        table.domain[gene.input_name].attributes[self.target_database] = \
                            str(gene.ncbi_id) if gene.ncbi_id else '?'

                        if self.replace_id_with_symbol:
                            try:
                                table.domain[gene.input_name].name = str(gene.symbol)
                            except AttributeError:
                                # TODO: missing gene symbol, need to handle this?
                                pass

                # filter selected columns
                selected = [column for column in table.domain.attributes
                            if self.target_database in column.attributes and
                            str(column.attributes[self.target_database]) in selected_genes]

                output_attrs = table.domain.attributes

                if selected:
                    output_attrs = selected

                if self.exclude_unmatched:
                    output_attrs = [col for col in output_attrs if col.attributes[self.target_database] in known_genes]

                domain = Domain(output_attrs,
                                table.domain.class_vars,
                                table.domain.metas)

                table = table.from_table(domain, table)

                # handle table attributes
                table.attributes[TAX_ID] = self.get_selected_organism()
                table.attributes[GENE_AS_ATTRIBUTE_NAME] = True
                table.attributes[GENE_ID_ATTRIBUTE] = self.target_database

            gm_table = self.gene_matcher.to_data_table(selected_genes=selected_genes if selected_genes else None)

        self.Outputs.data_table.send(table)
        self.Outputs.gene_matcher_results.send(gm_table)

    def toggle_radio_options(self):
        self.replace_radio.setEnabled(bool(self.use_attr_names))

        if self.gene_matcher.genes:
            # enable checkbox if unknown genes are detected
            self.exclude_radio.setEnabled(len(self.gene_matcher.genes) != len(self.gene_matcher.get_known_genes()))
            self.exclude_unmatched = len(self.gene_matcher.genes) != len(self.gene_matcher.get_known_genes())

    def get_target_ids(self):
        return [str(gene.ncbi_id) if gene.ncbi_id else '?' for gene in self.gene_matcher.genes]

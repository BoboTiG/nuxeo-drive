/*
 * (C) Copyright 2012 Nuxeo SA (http://nuxeo.com/) and contributors.
 *
 * All rights reserved. This program and the accompanying materials
 * are made available under the terms of the GNU Lesser General Public License
 * (LGPL) version 2.1 which accompanies this distribution, and is available at
 * http://www.gnu.org/licenses/lgpl.html
 *
 * This library is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
 * Lesser General Public License for more details.
 *
 * Contributors:
 *     Antoine Taillefer <ataillefer@nuxeo.com>
 */
package org.nuxeo.drive.adapter;

import java.util.Calendar;

import org.nuxeo.drive.adapter.impl.DocumentBackedFileItem;
import org.nuxeo.drive.adapter.impl.DocumentBackedFolderItem;
import org.nuxeo.ecm.core.api.ClientException;
import org.nuxeo.ecm.core.api.DocumentModel;

/**
 * {@link DocumentModel} backed implementation of a {@link FileSystemItem}.
 *
 * @author Antoine Taillefer
 * @see DocumentBackedFileItem
 * @see DocumentBackedFolderItem
 */
public abstract class AbstractDocumentBackedFileSystemItem implements
        FileSystemItem {

    protected final DocumentModel doc;

    protected AbstractDocumentBackedFileSystemItem(DocumentModel doc) {
        this.doc = doc;
    }

    /*--------------------- FileSystemItem ---------------------*/

    public abstract String getName() throws ClientException;

    public abstract boolean isFolder();

    public String getId() {
        return doc.getRepositoryName() + "/" + doc.getId();
    }

    public String getCreator() throws ClientException {
        return (String) doc.getPropertyValue("dc:creator");
    }

    public Calendar getCreationDate() throws ClientException {
        return (Calendar) doc.getPropertyValue("dc:created");
    }

    public Calendar getLastModificationDate() throws ClientException {
        return (Calendar) doc.getPropertyValue("dc:modified");
    }

    /*--------------------- AbstractDocumentBackedFileSystemItem ---------------------*/
    /**
     * Gets the backing {@link DocumentModel}. Possibly null if the file system
     * item has no representation server-side.
     */
    public DocumentModel getDocument() {
        return doc;
    }

}
